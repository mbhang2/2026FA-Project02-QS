"""
Absorption and initiation strategy with multi-bar delta confirmation.

Analysis of recent trials shows a clear pattern in what works:
- Trial #6: -0.1967 (158 trades) - Best overall baseline
- Trial #18: -0.1216 (34 trades) - Close positioning filter helped but still too strict
- Trial #17: -0.0838 (23 trades) - Close positioning against bar midpoint too restrictive
- Trial #16: -0.0125 (8 trades) - 3-bar requirement killed signal
- Trial #15: -0.0987 (33 trades) - Prior bar delta confirmation too strict

The core insight: Trial #6's strength came from:
1. Adaptive delta threshold at 75th percentile
2. Volume filter at 60% of average
3. Prior bar delta confirmation in absorption detection
4. 2-bar initiation window with 2-bar requirement
5. Solid risk/reward (stop=0.40, target=0.65)
6. 6-bar max hold period

The problem with #18: Filtering initiation bars by close position relative to
the absorption bar's prior close was still too restrictive (only 34 trades).

New approach for this trial:
1. Keep absorption detection from #6 exactly (prior bar delta confirmation works)
2. Simplify initiation: just require strong opposite delta, no close positioning
3. But add a "conviction" check on the initiation bar itself: it must show that
   the reversal is real by having a delta that is not just opposite but STRONGER
   than a baseline (e.g., > 80% of threshold rather than 65%)
4. This maintains signal frequency while filtering weak reversals
5. Add a multi-bar confirmation: after first initiation bar, require at least
   one more bar with delta in the same direction (not necessarily strong)
6. Keep 2-bar initiation window (tight but maintains signal)
7. Reduce initiation_bars_required back to 2 (was 2 in #18 anyway)
8. Adjust stop_offset to 0.41 (fine-tune between #6's 0.40 and #18's 0.42)
9. Keep target_offset at 0.65 (proven)
10. Keep max_hold_bars at 6 (good balance)

Expected outcome: ~120-150 trades with improved score over #6's -0.1967 by:
- Filtering weak reversals via stronger delta requirement
- Maintaining signal frequency (no close positioning constraint)
- Multi-bar conviction check adds robustness
- Better risk management overall
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
        stop_offset: float = 0.41,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 6,
        initiation_window: int = 2,
        initiation_bars_required: int = 2,
        initiation_delta_strength: float = 0.80,
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
        self.initiation_bars_required: int = initiation_bars_required
        self.initiation_delta_strength: float = initiation_delta_strength

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
        self.initiation_bar_count: int = 0
        self.last_initiation_delta_sign: Optional[int] = None

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
            self.initiation_bar_count = 0
            self.last_initiation_delta_sign = None
            return

        # Reset when flat
        self.bars_held = 0

        # Detect absorption on fast bars
        absorption = self._detect_absorption(fast_history)

        if absorption:
            self.absorption_signal = absorption
            self.bars_since_absorption = 0
            self.initiation_bar_count = 0
            self.last_initiation_delta_sign = None
            return

        # Check for initiation after absorption (within the window)
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            if self.bars_since_absorption <= self.initiation_window:
                if self._is_initiation_bar(fast_bar, self.absorption_signal):
                    # First initiation bar: set the sign
                    if self.initiation_bar_count == 0:
                        self.last_initiation_delta_sign = 1 if fast_bar.delta > 0 else -1
                    
                    self.initiation_bar_count += 1
                    
                    # Only enter after required number of initiation bars
                    if self.initiation_bar_count >= self.initiation_bars_required:
                        self._place_entry(ctx, self.absorption_signal)
                        self.absorption_signal = None
                        self.bars_since_absorption = 0
                        self.initiation_bar_count = 0
                        self.last_initiation_delta_sign = None
                else:
                    # Check if this bar continues the initiation direction (weaker requirement)
                    if self.initiation_bar_count > 0 and self._continues_initiation_direction(fast_bar):
                        self.initiation_bar_count += 1
                        
                        if self.initiation_bar_count >= self.initiation_bars_required:
                            self._place_entry(ctx, self.absorption_signal)
                            self.absorption_signal = None
                            self.bars_since_absorption = 0
                            self.initiation_bar_count = 0
                            self.last_initiation_delta_sign = None
                    else:
                        # Reset counter if bar doesn't continue the direction
                        self.initiation_bar_count = 0
                        self.last_initiation_delta_sign = None
            else:
                # Reset if we exceed the initiation window
                self.absorption_signal = None
                self.bars_since_absorption = 0
                self.initiation_bar_count = 0
                self.last_initiation_delta_sign = None

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
        Check if bar shows strong initiation: opposite delta with high conviction.
        
        For buy absorption (we want to sell): need strong negative delta
        For sell absorption (we want to buy): need strong positive delta
        
        The initiation must show strong conviction (higher delta strength threshold).
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta
            if bar.delta < -threshold * self.initiation_delta_strength:
                return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta
            if bar.delta > threshold * self.initiation_delta_strength:
                return True

        return False

    def _continues_initiation_direction(self, bar) -> bool:
        """
        Check if bar continues the initiation direction (weaker requirement).
        
        Just needs to have delta in the same direction as the first initiation bar,
        doesn't need to be strong.
        """
        if self.last_initiation_delta_sign is None:
            return False

        if self.last_initiation_delta_sign > 0:
            return bar.delta > 0
        else:
            return bar.delta < 0

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
