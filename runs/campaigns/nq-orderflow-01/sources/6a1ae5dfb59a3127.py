"""
Absorption and initiation strategy with refined entry timing and reduced latency.

Analysis of trial progression:
- Trial #6: -0.1967 (158 trades) - BEST overall performance
- Trial #12: -0.0357 (8 trades) - Over-filtered to near silence
- Trial #11: -0.2212 (167 trades) - Close to #6 but slightly worse
- Trial #3: -0.2164 (191 trades) - Earlier good version

The pattern is clear: Trial #6 had the optimal balance. The attempts to add
more filters (reversal magnitude, close positioning, multi-bar confirmation)
have either regressed or drastically over-filtered.

Key insight: Trial #6 worked because it was SIMPLE but EFFECTIVE:
- Lookback=30, percentile=0.75 (stable thresholds)
- volume_threshold=0.60 (good balance)
- stop_offset=0.40, target_offset=0.65 (solid risk/reward)
- initiation_window=3, initiation_bars_required=2 (reasonable confirmation)
- The absorption detection with prior bar confirmation filtered noise well

The problem with later trials:
- Trial #7 added too many filters (close positioning, strength checks) → 26 trades
- Trial #12 added reversal magnitude requirement → 8 trades (catastrophic)
- These "improvements" were actually over-constraints

New hypothesis for this trial:
Trial #6 was nearly optimal. The small regressions in #11 suggest we need to
look at TIMING and ENTRY PRECISION, not MORE FILTERS.

Key refinement: REDUCE LATENCY
1. Return to trial #6's core parameters exactly
2. The initiation window of 3 bars with 2 bars required means we enter on bar 2
   after absorption. What if we're entering too late?
3. Instead of requiring 2 consecutive initiation bars, require just 1 strong one
   but make the initiation check more precise
4. Absorption detection: keep the prior bar confirmation (it works)
5. Initiation detection: simplified - just strong opposite delta, no position checks
6. Remove the "strength threshold" check - the main threshold is sufficient
7. Tighter max_hold: 7 bars instead of 8 (reduce commission drag)
8. Keep brackets at trial #6 levels (0.40/0.65)

The core insight: Trial #6 was RIGHT. We've been trying to improve it by adding
constraints, but each constraint has made things worse. The answer is to
SIMPLIFY back to what worked, with only micro-optimizations:
- Slightly faster entry (1 initiation bar instead of 2)
- Slightly tighter hold (7 bars instead of 8)
- Slightly tighter stop (0.38 instead of 0.40)

This should maintain the ~158 trade volume that worked well while improving
entry timing and reducing commission drag.
"""

from collections import deque
from typing import Deque, Optional


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        lookback: int = 30,
        delta_percentile: float = 0.75,
        volume_threshold: float = 0.60,
        stop_offset: float = 0.38,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 7,
        initiation_window: int = 3,
    ) -> None:
        self.fast_interval: str = fast_interval
        self.slow_interval: str = slow_interval
        self.lookback: int = lookback
        self.delta_percentile: float = delta_percentile
        self.volume_threshold: float = volume_threshold
        self.stop_offset: float = stop_offset
        self.target_offset: float = target_offset
        self.size: int = size
        self.max_hold_bars: int = max_hold_bars
        self.initiation_window: int = initiation_window

        # Fast (1m) history
        self.fast_delta_history: Deque[int] = deque(maxlen=lookback)
        self.fast_volume_history: Deque[int] = deque(maxlen=lookback)
        self.fast_high_history: Deque[float] = deque(maxlen=lookback)
        self.fast_low_history: Deque[float] = deque(maxlen=lookback)
        self.fast_close_history: Deque[float] = deque(maxlen=lookback)

        # Signal tracking
        self.absorption_signal: Optional[str] = None
        self.bars_since_absorption: int = 0
        self.bars_held: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Get fast bars
        fast_history = ctx.history(self.fast_interval)
        if len(fast_history) < 1:
            return

        fast_bar = fast_history[-1]

        # Update fast history
        self.fast_delta_history.append(fast_bar.delta)
        self.fast_volume_history.append(fast_bar.volume)
        self.fast_high_history.append(fast_bar.high)
        self.fast_low_history.append(fast_bar.low)
        self.fast_close_history.append(fast_bar.close)

        # Position management
        if ctx.position:
            self.bars_held += 1
            # Exit after max hold period
            if self.bars_held >= self.max_hold_bars:
                ctx.close()
            self.absorption_signal = None
            self.bars_since_absorption = 0
            return

        # Reset when flat
        self.bars_held = 0

        # Detect absorption on fast bars
        absorption = self._detect_absorption(fast_history)

        if absorption:
            self.absorption_signal = absorption
            self.bars_since_absorption = 0
            return

        # Check for initiation after absorption (within the window)
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            if self.bars_since_absorption <= self.initiation_window:
                if self._is_initiation_bar(fast_bar, self.absorption_signal):
                    self._place_entry(ctx, self.absorption_signal)
                    self.absorption_signal = None
                    self.bars_since_absorption = 0
            else:
                # Reset if we exceed the initiation window
                self.absorption_signal = None
                self.bars_since_absorption = 0

    def _get_delta_threshold(self) -> float:
        """
        Compute delta threshold using percentile of recent absolute deltas.
        """
        if len(self.fast_delta_history) < self.lookback // 2:
            return 50.0

        abs_deltas = sorted([abs(d) for d in self.fast_delta_history])
        idx = int(len(abs_deltas) * self.delta_percentile)
        idx = max(0, min(idx, len(abs_deltas) - 1))
        threshold: float = float(abs_deltas[idx])

        return max(threshold, 50.0)

    def _get_avg_volume(self) -> float:
        """Get average volume from recent bars."""
        if not self.fast_volume_history:
            return 100.0
        return sum(self.fast_volume_history) / len(self.fast_volume_history)

    def _detect_absorption(self, history) -> Optional[str]:
        """
        Detect absorption: large delta that fails to extend price extremes.
        
        Buy absorption: Large positive delta (buyers aggressive) but fails to
        make a new high relative to prior bar, AND closes below prior close.
        Confirm with prior bar showing positive delta (trend attempt).
        
        Sell absorption: Large negative delta (sellers aggressive) but fails to
        make a new low relative to prior bar, AND closes above prior close.
        Confirm with prior bar showing negative delta (trend attempt).
        """
        if len(history) < 2:
            return None

        last = history[-1]
        prior = history[-2]

        # Volume filter
        avg_vol = self._get_avg_volume()
        if last.volume < avg_vol * self.volume_threshold:
            return None

        threshold = self._get_delta_threshold()

        # Buy absorption: large positive delta but fails to extend high
        if last.delta > threshold:
            # Must fail to make new high AND close below prior close
            if last.high <= prior.high and last.close < prior.close:
                # Confirm with prior bar showing positive delta (trend attempt)
                if prior.delta > 0:
                    return "buy_absorption"

        # Sell absorption: large negative delta but fails to extend low
        if last.delta < -threshold:
            # Must fail to make new low AND close above prior close
            if last.low >= prior.low and last.close > prior.close:
                # Confirm with prior bar showing negative delta (trend attempt)
                if prior.delta < 0:
                    return "sell_absorption"

        return None

    def _is_initiation_bar(self, bar, absorption_signal: str) -> bool:
        """
        Check if bar shows initiation: opposite delta flow.
        Simplified: just require strong opposite delta.
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta
            return bar.delta < -threshold

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta
            return bar.delta > threshold

        return False

    def _place_entry(self, ctx, absorption_signal: str) -> None:
        """Place entry order based on absorption signal."""
        if absorption_signal == "buy_absorption":
            # Absorption of buying = sell initiation
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-sell",
            )
        elif absorption_signal == "sell_absorption":
            # Absorption of selling = buy initiation
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-buy",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
