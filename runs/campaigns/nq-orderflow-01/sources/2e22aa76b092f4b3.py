"""
Absorption and initiation strategy with refined volume and momentum analysis.

Analysis of the trial progression shows that selectivity is key:
- Trial #6: -0.1967 (158 trades) - Baseline
- Trial #35: -0.1490 (42 trades) - High quality
- Trial #39: -0.1164 (32 trades) - Best quality
- Trial #42: -0.0125 (8 trades) - Too selective, lost signal

Trial #42 was too restrictive with the close conviction requirement combined with
high delta strength (0.85). The close position filter eliminated too many valid
signals.

Key insight: The sweet spot is around 30-40 trades with -0.11 to -0.12 score.
This suggests we need:
1. Strong absorption detection (proven in #40/#42)
2. Initiation bars that show conviction but not BOTH delta strength AND close position
3. A slightly wider initiation window to catch the best reversals

New approach for this trial:
1. Keep absorption detection from #42 (delta threshold, volume filter, prior delta)
2. For initiation: require EITHER strong delta (0.80 threshold) OR good close position
   (not both - this was killing #42)
3. Add a refinement: track the absorption bar's delta magnitude to scale initiation
   requirements - stronger absorption bars need stronger initiation
4. Keep all proven exit parameters: stop_offset=0.40, target_offset=0.65, max_hold_bars=6
5. Use initiation_window=4 (between #42's 3 and #41's 5)

The key difference: we're using an OR condition for initiation conviction rather than
AND, which should recover some of the lost signal from #42 while maintaining quality.

Expected outcome: 25-40 trades with score around -0.1150 to -0.1300.
"""

from collections import deque
from typing import Deque, Optional


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        lookback: int = 30,
        delta_percentile: float = 0.80,
        volume_threshold: float = 0.60,
        stop_offset: float = 0.40,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 6,
        initiation_window: int = 4,
        initiation_delta_strength: float = 0.80,
        prior_delta_threshold: float = 32.0,
        close_quartile_threshold: float = 0.33,
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
        self.close_quartile_threshold: float = close_quartile_threshold

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
        self.absorption_delta_magnitude: int = 0

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
            self.absorption_delta_magnitude = abs(fast_history[-1].delta)
            return

        # Check for initiation after absorption (within the window)
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            if self.bars_since_absorption <= self.initiation_window:
                # Check if this bar shows initiation with flexible conviction
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

    def _is_close_in_lower_portion(self, bar) -> bool:
        """Check if close is in lower portion of bar range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False
        close_position = (bar.close - bar.low) / bar_range
        return close_position < self.close_quartile_threshold

    def _is_close_in_upper_portion(self, bar) -> bool:
        """Check if close is in upper portion of bar range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False
        close_position = (bar.close - bar.low) / bar_range
        return close_position > (1.0 - self.close_quartile_threshold)

    def _detect_absorption(self, history) -> Optional[str]:
        """
        Detect absorption: large delta that fails to extend price extremes.
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
                    # Additional check: close in lower portion of range
                    if self._is_close_in_lower_portion(last):
                        return "buy_absorption"

        # Sell absorption: large negative delta but fails to extend low
        if last.delta < -threshold:
            # Must fail to make new low AND close above prior close
            if last.low >= prior.low and last.close > prior.close:
                # Confirm with prior bar showing meaningful negative delta
                if prior.delta < -self.prior_delta_threshold:
                    # Additional check: close in upper portion of range
                    if self._is_close_in_upper_portion(last):
                        return "sell_absorption"

        return None

    def _is_initiation_bar(self, bar, absorption_signal: str) -> bool:
        """
        Check if bar shows initiation with flexible conviction.
        Use OR logic: either strong delta OR good close position (not both required).
        
        For buy absorption (we want to sell): 
          - Need negative delta at 0.80 of threshold
          - OR close in upper portion (seller conviction)
        
        For sell absorption (we want to buy):
          - Need positive delta at 0.80 of threshold
          - OR close in lower portion (buyer conviction)
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta OR upper close
            has_strong_delta = bar.delta < -threshold * self.initiation_delta_strength
            has_conviction_close = self._is_close_in_upper_portion(bar)
            
            # Return true if EITHER condition is met
            if has_strong_delta or has_conviction_close:
                # But require at least some negative delta
                if bar.delta < 0:
                    return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta OR lower close
            has_strong_delta = bar.delta > threshold * self.initiation_delta_strength
            has_conviction_close = self._is_close_in_lower_portion(bar)
            
            # Return true if EITHER condition is met
            if has_strong_delta or has_conviction_close:
                # But require at least some positive delta
                if bar.delta > 0:
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
