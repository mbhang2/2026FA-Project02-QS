"""
Absorption and initiation strategy with refined entry timing and better signal confirmation.

Prior trials analysis:
- Trial #3: -0.2164 (best) - Good absorption detection, simple initiation
- Trial #5: -0.2066 - Similar to #3, slightly worse
- Trial #4: -0.3203 - Too many filters, regressed
- Trial #2: -0.3583 - Fewer trades, worse performance

Key observations from #3 (best performer):
1. Simpler logic works better than complex multi-filter approaches
2. 10-bar hold period with bracket exits is effective
3. 0.40 stop, 0.65 target is a good risk/reward
4. Absorption detection with "fails to extend" + "closes opposite" works

Issues to address:
1. The downtrend check in #5 may be too aggressive and filtering valid signals
2. The 0.45 threshold for initiation bar close position might be filtering winners
3. Entry timing: we wait 1-2 bars after absorption. What if we're too late?
4. The initiation bar requirements are strict - maybe we need more flexibility

Refinements for this iteration:
1. Remove the "recent downtrend" filter - it's likely too restrictive
2. Loosen initiation detection: accept initiation bars that move in the right
   direction even if not perfectly positioned (0.40 instead of 0.45)
3. Increase lookback to 25 for better delta percentile statistics
4. Lower volume threshold slightly to 0.60 (was 0.65) to catch more signals
5. Extend initiation window to 3 bars (was 2) - give more time for reversal
6. Keep max_hold at 10 bars (not 8) - let winning trades run longer
7. Revert to 0.40 stop, 0.65 target (what worked in #3)
8. Add a filter: only take absorption if the bar before shows agreement with
   the absorption direction (positive delta for buy absorption, negative for sell)
   This confirms we're seeing a real reversal pattern.
9. Simplify: remove the momentum comparison, just require opposite direction delta
"""

from collections import deque
from typing import Deque, Optional


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        lookback: int = 25,
        delta_percentile: float = 0.75,
        volume_threshold: float = 0.60,
        stop_offset: float = 0.40,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 10,
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
                if self._detect_initiation(fast_bar, self.absorption_signal):
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

    def _detect_initiation(self, bar, absorption_signal: str) -> bool:
        """
        Detect initiation: opposite delta flow that moves price in that direction.
        
        Initiation must:
        1. Have opposite delta from absorption signal (exceed threshold)
        2. Move price in the direction of the delta (close positioning)
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta + closes in lower portion
            if bar.delta < -threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0:
                    # Close in lower 40% of range (price moved down with selling)
                    close_to_low = (bar.high - bar.close) / bar_range
                    if close_to_low > 0.40:
                        return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta + closes in upper portion
            if bar.delta > threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0:
                    # Close in upper 40% of range (price moved up with buying)
                    close_to_high = (bar.close - bar.low) / bar_range
                    if close_to_high > 0.40:
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
