"""
Absorption and initiation strategy with multi-bar confirmation and adaptive exits.

Trial #6 analysis (best at -0.1967):
- 158 trades, good profitability
- Prior delta confirmation in absorption detection helps filter noise
- 3-bar initiation window allows more flexibility
- 10-bar max hold is appropriate

Key insight from trial progression:
- Trial #3: -0.2164 (191 trades) - simpler, worked well
- Trial #6: -0.1967 (158 trades) - fewer trades but better quality
- The quality improvement suggests stricter entry filters are better

Current hypothesis for improvement:
1. The absorption signal is good, but initiation detection is still loose
2. We're getting into trades that don't work out well
3. We need to require initiation bar to show STRONGER conviction
4. The bar_range > 0 check is trivial - all bars have some range
5. Consider: what if we require initiation bar to exceed threshold by MORE?

Refinements for this iteration:

1. Tighten initiation: require initiation bar delta to be in top percentile
   (stronger than just exceeding threshold). This ensures real reversal conviction.

2. Add multi-bar confirmation: don't enter on first initiation bar alone.
   Require at least 2 bars in a row showing the right direction delta.
   This filters one-bar noise spikes.

3. Improve close positioning: instead of just 40% threshold, require the
   initiation bar to close in the extreme 33% of its range (more aggressive).

4. Adjust hold period: reduce from 10 to 8 bars - we're paying commission
   and the best trades likely exit earlier anyway.

5. Tighten brackets: 0.35 stop, 0.60 target (was 0.40/0.65) - better risk/reward
   and less likely to be stopped on noise.

6. Increase lookback to 30 for more stable delta percentile calculation.

7. Add a "signal strength" requirement: absorption bar delta must be in top 50%
   of absolute deltas to qualify (not just exceed threshold).

8. Track consecutive initiation bars: maintain counter of bars showing
   opposite delta direction, only enter when counter >= 2.
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
        stop_offset: float = 0.35,
        target_offset: float = 0.60,
        size: int = 1,
        max_hold_bars: int = 8,
        initiation_window: int = 3,
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
                    # Only enter after required number of consecutive initiation bars
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

    def _get_delta_strength_threshold(self) -> float:
        """
        Compute 50th percentile of absolute deltas for signal strength check.
        """
        if len(self.fast_delta_history) < self.lookback // 2:
            return 30.0

        abs_deltas = sorted([abs(d) for d in self.fast_delta_history])
        idx = int(len(abs_deltas) * 0.50)
        idx = max(0, min(idx, len(abs_deltas) - 1))
        threshold: float = float(abs_deltas[idx])

        return max(threshold, 30.0)

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
        Also require absorption delta to be in top 50% of strength.
        
        Sell absorption: Large negative delta (sellers aggressive) but fails to
        make a new low relative to prior bar, AND closes above prior close.
        Confirm with prior bar showing negative delta (trend attempt).
        Also require absorption delta to be in top 50% of strength.
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
        strength_threshold = self._get_delta_strength_threshold()

        # Buy absorption: large positive delta but fails to extend high
        if last.delta > threshold and abs(last.delta) >= strength_threshold:
            # Must fail to make new high AND close below prior close
            if last.high <= prior.high and last.close < prior.close:
                # Confirm with prior bar showing positive delta (trend attempt)
                if prior.delta > 0:
                    return "buy_absorption"

        # Sell absorption: large negative delta but fails to extend low
        if last.delta < -threshold and abs(last.delta) >= strength_threshold:
            # Must fail to make new low AND close above prior close
            if last.low >= prior.low and last.close > prior.close:
                # Confirm with prior bar showing negative delta (trend attempt)
                if prior.delta < 0:
                    return "sell_absorption"

        return None

    def _is_initiation_bar(self, bar, absorption_signal: str) -> bool:
        """
        Check if bar shows initiation: opposite delta flow that moves price decisively.
        
        Initiation must:
        1. Have opposite delta from absorption signal (exceed threshold)
        2. Move price decisively in the direction of the delta (close in extreme 33%)
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta + closes in lower portion
            if bar.delta < -threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0.01:  # Avoid division issues with tiny ranges
                    # Close in lower 33% of range (price moved down decisively with selling)
                    close_to_low = (bar.high - bar.close) / bar_range
                    if close_to_low > 0.33:
                        return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta + closes in upper portion
            if bar.delta > threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0.01:  # Avoid division issues with tiny ranges
                    # Close in upper 33% of range (price moved up decisively with buying)
                    close_to_high = (bar.close - bar.low) / bar_range
                    if close_to_high > 0.33:
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
