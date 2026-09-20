"""
Absorption and initiation strategy with refined multi-bar momentum confirmation.

Analysis of trial progression reveals critical patterns:

Best performers:
- Trial #6: -0.1967 (158 trades) - Best overall
- Trial #47: -0.2162 (103 trades) - Recent with time filtering
- Trial #45: -0.1416 (50 trades) - Good per-trade quality
- Trial #39: -0.1164 (32 trades) - Excellent efficiency

Key insight from #6's success: 158 trades at -0.1967 beats 32 trades at -0.1164.
This suggests the signal is robust and scaling it up (more trades) is better than
filtering it down (fewer, higher-quality trades).

However, trial #47's time-of-day filtering (avoiding first/last 30 min) degraded
performance to -0.2162, suggesting those periods contain tradeable signal.

New hypothesis: The issue is not the number of trades or the time window, but
the QUALITY of initiation confirmation. Trial #6 used simple strong opposite delta.
We've been over-complicating initiation detection.

Refined approach:
1. Keep absorption detection from #46/#47: delta percentile 0.82, volume filter 0.60
2. Keep prior bar delta confirmation: explicit threshold of 40.0
3. SIMPLIFY initiation: require 2 consecutive bars with opposite strong delta
   (not 3 bars, not graduated thresholds - just 2 bars of conviction)
4. Track initiation bars more carefully: count bars showing the opposite delta
5. Remove time-of-day filtering (trial #47 showed it hurt performance)
6. Keep proven exits: stop_offset=0.40, target_offset=0.65, max_hold_bars=6
7. Reduce initiation_delta_strength to 0.65 (be less picky on reversal strength)
8. Tighter initiation_window=2 (force faster entry once signal appears)

The hypothesis: Trial #6's success came from:
- Capturing the absorption pattern reliably
- Entering quickly on the first strong reversal bar
- Not over-filtering with complex multi-bar confirmation

By requiring just 2 consecutive initiation bars (not 3) and lowering the delta
strength threshold, we capture more of the signal while maintaining selectivity.

Expected outcome: 120-160 trades with score -0.1900 to -0.2050.
"""

from collections import deque
from typing import Deque, Optional


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        lookback: int = 30,
        delta_percentile: float = 0.82,
        volume_threshold: float = 0.60,
        stop_offset: float = 0.40,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 6,
        initiation_window: int = 2,
        initiation_delta_strength: float = 0.65,
        prior_delta_threshold: float = 40.0,
        initiation_bars_required: int = 2,
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
        self.initiation_delta_strength: float = initiation_delta_strength
        self.prior_delta_threshold: float = prior_delta_threshold
        self.initiation_bars_required: int = initiation_bars_required

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
            return

        # Reset when flat
        self.bars_held = 0

        # Detect absorption on fast bars
        absorption = self._detect_absorption(fast_history)

        if absorption:
            self.absorption_signal = absorption
            self.bars_since_absorption = 0
            self.initiation_bar_count = 0
            return

        # Check for initiation after absorption (within the window)
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            if self.bars_since_absorption <= self.initiation_window:
                if self._is_initiation_bar(fast_bar, self.absorption_signal):
                    self.initiation_bar_count += 1
                    # Enter after required number of consecutive initiation bars
                    if self.initiation_bar_count >= self.initiation_bars_required:
                        self._place_entry(ctx, self.absorption_signal)
                        self.absorption_signal = None
                        self.bars_since_absorption = 0
                        self.initiation_bar_count = 0
                else:
                    # Reset counter if bar doesn't show initiation
                    self.initiation_bar_count = 0
            else:
                # Reset if we exceed the initiation window
                self.absorption_signal = None
                self.bars_since_absorption = 0
                self.initiation_bar_count = 0

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
        Confirm with prior bar showing positive delta.
        
        Sell absorption: Large negative delta (sellers aggressive) but fails to
        make a new low relative to prior bar, AND closes above prior close.
        Confirm with prior bar showing negative delta.
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
                # Confirm with prior bar showing positive delta
                if prior.delta > self.prior_delta_threshold:
                    return "buy_absorption"

        # Sell absorption: large negative delta but fails to extend low
        if last.delta < -threshold:
            # Must fail to make new low AND close above prior close
            if last.low >= prior.low and last.close > prior.close:
                # Confirm with prior bar showing negative delta
                if prior.delta < -self.prior_delta_threshold:
                    return "sell_absorption"

        return None

    def _is_initiation_bar(self, bar, absorption_signal: str) -> bool:
        """
        Check if bar shows initiation: strong opposite delta flow.
        
        For buy absorption (we want to sell): need strong negative delta
        For sell absorption (we want to buy): need strong positive delta
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
