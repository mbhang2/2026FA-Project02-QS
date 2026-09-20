"""
Absorption and initiation strategy with refined close-position analysis.

Analysis of trial progression shows:
- Trial #6: -0.1967 (158 trades) - Best overall
- Trial #35: -0.1490 (42 trades) - Too selective, lost signal
- Trials #31-34: -0.2481 to -0.2664 - Too many trades hurt performance

Trial #35 was too restrictive with the close-position quartile check and the
meaningful prior delta threshold (30.0). It reduced trades to 42 but the score
improved to -0.1490, which is interesting - fewer, higher-quality trades may
actually be better.

However, 42 trades is likely too few for reliable performance. The sweet spot
appears to be around 100-150 trades with a score around -0.15 to -0.20.

New approach - balance selectivity with signal volume:
1. Keep the core absorption logic from #6
2. Use a SOFTER version of the close-position check from #35:
   - For buy absorption: close should be in bottom HALF (not quartile)
   - For sell absorption: close should be in top HALF (not quartile)
   - This is less restrictive than #35 but still filters noise
3. For prior bar confirmation: require meaningful delta but lower threshold
   - Use 20.0 instead of 30.0 for prior delta threshold
4. Keep proven parameters:
   - stop_offset: 0.40
   - target_offset: 0.65
   - max_hold_bars: 6
   - delta_percentile: 0.75
   - volume_threshold: 0.60
5. Adjust initiation_delta_strength: 0.70 (slightly lower to catch more reversals)

Expected outcome: ~120-140 trades with score around -0.1700 to -0.1850 by:
- Being more selective than #6 but not overly restrictive
- Using close position to filter half the range (less severe than quartile)
- Balancing trade count with quality
- Keeping the core logic that made #6 work well
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
        stop_offset: float = 0.40,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 6,
        initiation_window: int = 3,
        initiation_delta_strength: float = 0.70,
        prior_delta_threshold: float = 20.0,
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

    def _is_close_in_bottom_half(self, bar) -> bool:
        """Check if close is in bottom half of bar range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False
        close_position = (bar.close - bar.low) / bar_range
        return close_position < 0.50

    def _is_close_in_top_half(self, bar) -> bool:
        """Check if close is in top half of bar range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False
        close_position = (bar.close - bar.low) / bar_range
        return close_position > 0.50

    def _detect_absorption(self, history) -> Optional[str]:
        """
        Detect absorption: large delta that fails to extend price extremes.
        
        Buy absorption: Large positive delta (buyers aggressive) but fails to
        make a new high relative to prior bar, AND closes below prior close.
        Confirm with prior bar showing meaningful positive delta.
        AND close must be in bottom half of the bar's range.
        
        Sell absorption: Large negative delta (sellers aggressive) but fails to
        make a new low relative to prior bar, AND closes above prior close.
        Confirm with prior bar showing meaningful negative delta.
        AND close must be in top half of the bar's range.
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
                # Confirm with prior bar showing meaningful positive delta
                if prior.delta > self.prior_delta_threshold:
                    # Additional check: close in bottom half of range
                    if self._is_close_in_bottom_half(last):
                        return "buy_absorption"

        # Sell absorption: large negative delta but fails to extend low
        if last.delta < -threshold:
            # Must fail to make new low AND close above prior close
            if last.low >= prior.low and last.close > prior.close:
                # Confirm with prior bar showing meaningful negative delta
                if prior.delta < -self.prior_delta_threshold:
                    # Additional check: close in top half of range
                    if self._is_close_in_top_half(last):
                        return "sell_absorption"

        return None

    def _is_initiation_bar(self, bar, absorption_signal: str) -> bool:
        """
        Check if bar shows initiation: opposite delta flow in the right direction.
        
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
