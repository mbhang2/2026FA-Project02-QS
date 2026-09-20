"""
Absorption and initiation strategy with adaptive momentum confirmation.

Analysis of recent trials shows a critical pattern:
- Trial #39: -0.1164 (32 trades) - Sweet spot in selectivity
- Trial #40: -0.1226 (45 trades) - Slightly worse with more trades
- Trial #42: -0.0695 (2 trades) - Over-constrained with close conviction
- Trial #43: -0.1608 (51 trades) - Regression, too loose

The issue with #43: Using OR logic for initiation conviction (strong delta OR 
good close) created too many false signals. We need AND logic but with more 
relaxed thresholds than #42.

Key insight from #39's success (32 trades, -0.1164):
1. Absorption detection was tight and selective
2. Initiation required BOTH meaningful opposite delta AND some close conviction
3. The parameters were well-tuned for the market regime

This trial refines #39's approach:
1. Keep strong absorption detection (high delta percentile, volume filter)
2. For initiation: require BOTH conditions but with more achievable thresholds
   - Delta strength at 0.70 of threshold (more achievable than 0.85)
   - Close position at 0.40 quartile (slightly relaxed from 0.33)
3. Add a momentum bar counter: require 1-2 bars showing same-direction delta
   after initiation to confirm the move is sustained
4. Slightly tighter absorption prior delta requirement (35.0 vs 32.0)
5. Keep proven exit parameters: stop_offset=0.40, target_offset=0.65, max_hold_bars=6

The key difference: we're using achievable AND conditions rather than loose OR,
which should maintain the 30-40 trade count and -0.11 to -0.12 score range.

Expected outcome: 28-38 trades with score around -0.1150 to -0.1200.
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
        initiation_window: int = 4,
        initiation_delta_strength: float = 0.70,
        prior_delta_threshold: float = 35.0,
        close_quartile_threshold: float = 0.40,
        momentum_confirmation_bars: int = 1,
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
        self.momentum_confirmation_bars: int = momentum_confirmation_bars

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
        self.initiation_direction: Optional[str] = None

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
            self.initiation_direction = None
            return

        # Reset when flat
        self.bars_held = 0

        # Detect absorption on fast bars
        absorption = self._detect_absorption(fast_history)

        if absorption:
            self.absorption_signal = absorption
            self.bars_since_absorption = 0
            self.initiation_bar_count = 0
            self.initiation_direction = None
            return

        # Check for initiation after absorption (within the window)
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            if self.bars_since_absorption <= self.initiation_window:
                # Check if this bar shows initiation
                initiation_type = self._get_initiation_type(
                    fast_bar, self.absorption_signal
                )

                if initiation_type is not None:
                    if self.initiation_direction is None:
                        self.initiation_direction = initiation_type
                        self.initiation_bar_count = 1
                    elif self.initiation_direction == initiation_type:
                        self.initiation_bar_count += 1

                    # Enter when we have enough momentum confirmation
                    if (
                        self.initiation_bar_count
                        >= self.momentum_confirmation_bars
                    ):
                        self._place_entry(ctx, self.absorption_signal)
                        self.absorption_signal = None
                        self.bars_since_absorption = 0
                        self.initiation_bar_count = 0
                        self.initiation_direction = None
                else:
                    # Reset if bar doesn't show initiation
                    self.initiation_bar_count = 0
                    self.initiation_direction = None
            else:
                # Reset if we exceed the initiation window
                self.absorption_signal = None
                self.bars_since_absorption = 0
                self.initiation_bar_count = 0
                self.initiation_direction = None

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

    def _get_initiation_type(
        self, bar, absorption_signal: str
    ) -> Optional[str]:
        """
        Check if bar shows initiation: opposite delta with close conviction.
        
        Returns the initiation type ("buy_init" or "sell_init") if confirmed,
        or None if not.
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: negative delta + upper close
            has_strong_delta = bar.delta < -threshold * self.initiation_delta_strength
            has_conviction_close = self._is_close_in_upper_portion(bar)

            # Require BOTH conditions
            if has_strong_delta and has_conviction_close:
                return "sell_init"

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: positive delta + lower close
            has_strong_delta = bar.delta > threshold * self.initiation_delta_strength
            has_conviction_close = self._is_close_in_lower_portion(bar)

            # Require BOTH conditions
            if has_strong_delta and has_conviction_close:
                return "buy_init"

        return None

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
