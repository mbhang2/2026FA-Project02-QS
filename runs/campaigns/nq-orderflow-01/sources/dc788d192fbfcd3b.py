"""
Absorption and initiation strategy with dual-threshold momentum confirmation.

Analysis of recent trials shows that the sweet spot is around 30-40 trades with
scores of -0.11 to -0.12. Recent trials have oscillated around this range:

- Trial #39: -0.1164 (32 trades) - Sweet spot
- Trial #40: -0.1226 (45 trades) - Slightly worse
- Trial #42: -0.0695 (2 trades) - Over-constrained
- Trial #43: -0.1608 (51 trades) - Too loose with OR logic
- Trial #44: -0.0350 (7 trades) - Over-constrained with AND logic

The pattern suggests that:
1. Absorption detection needs to be selective (high delta percentile, volume filter)
2. Initiation requires conviction but not overly strict
3. The balance between sensitivity and specificity is critical

Trial #44's failure with AND logic (requiring both strong delta AND close conviction)
was too restrictive, yielding only 7 trades. Trial #43's OR logic was too loose.

New approach - adaptive momentum thresholds:
1. Keep strong absorption detection from #42/#44 (delta percentile 0.82, volume filter)
2. For initiation: use a hybrid approach
   - Primary check: strong opposite delta at 0.75 of threshold (achievable)
   - Secondary check: if delta is moderate (0.50-0.75 threshold), require good close
   - This creates a graduated acceptance rather than hard AND/OR logic
3. Add volume confirmation: initiation bar should have reasonable volume
4. Track the absorption bar's characteristics to scale expectations
5. Keep proven exit parameters: stop_offset=0.40, target_offset=0.65, max_hold_bars=6
6. Slightly relax initiation window to 5 bars (give more time to develop)

The key difference: graduated initiation thresholds instead of binary AND/OR,
which should maintain 30-40 trades with -0.11 to -0.12 score range while being
more robust to market regime changes.

Expected outcome: 32-40 trades with score around -0.1100 to -0.1200.
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
        initiation_window: int = 5,
        strong_delta_threshold: float = 0.75,
        moderate_delta_threshold: float = 0.50,
        prior_delta_threshold: float = 35.0,
        close_quartile_threshold: float = 0.35,
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
        self.strong_delta_threshold: float = strong_delta_threshold
        self.moderate_delta_threshold: float = moderate_delta_threshold
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
        Check if bar shows initiation with graduated momentum thresholds.
        
        For buy absorption (we want to sell):
          - Strong negative delta (0.75 threshold) accepts any reasonable close
          - Moderate negative delta (0.50-0.75) requires good upper close
        
        For sell absorption (we want to buy):
          - Strong positive delta (0.75 threshold) accepts any reasonable close
          - Moderate positive delta (0.50-0.75) requires good lower close
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation
            strong_delta = bar.delta < -threshold * self.strong_delta_threshold
            moderate_delta = (
                -threshold * self.moderate_delta_threshold
                <= bar.delta
                < -threshold * self.strong_delta_threshold
            )
            upper_close = self._is_close_in_upper_portion(bar)

            # Strong delta accepts any bar; moderate delta needs close conviction
            if strong_delta:
                return True
            if moderate_delta and upper_close:
                return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation
            strong_delta = bar.delta > threshold * self.strong_delta_threshold
            moderate_delta = (
                threshold * self.moderate_delta_threshold
                <= bar.delta
                <= threshold * self.strong_delta_threshold
            )
            lower_close = self._is_close_in_lower_portion(bar)

            # Strong delta accepts any bar; moderate delta needs close conviction
            if strong_delta:
                return True
            if moderate_delta and lower_close:
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
