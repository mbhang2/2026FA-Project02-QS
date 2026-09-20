"""
Absorption and initiation strategy with momentum-based confirmation.

Analysis of recent trials shows a critical pattern:
- Trial #6: -0.1967 (158 trades) - Best overall baseline
- Trial #35: -0.1490 (42 trades) - High quality, very selective
- Trial #39: -0.1164 (32 trades) - Even more selective
- Trial #40: -0.1226 (45 trades) - Slight regression

The trend is clear: MORE SELECTIVE is BETTER. Trials with 30-50 trades score
significantly better than trials with 150+ trades. This suggests that:

1. Absorption + initiation signals are RARE and HIGH QUALITY when they occur
2. The more we filter, the better the signal-to-noise ratio
3. Commission drag is a major factor - fewer, better trades beat many mediocre ones

Key insight from the data:
- #35 and #39 achieved -0.11 to -0.15 with just 30-45 trades
- This is 3-5x better per trade than #6's 158 trades at -0.1967
- The issue isn't finding signals; it's filtering ruthlessly

New hypothesis: We need MULTI-BAR MOMENTUM CONFIRMATION.
Instead of just checking if initiation bar has opposite delta, require that
the initiation bar AND the next bar BOTH show momentum in the same direction.
This creates a two-bar momentum confirmation that's much more selective.

Implementation:
1. Keep absorption detection from #40 (proven to work)
2. For initiation: require the bar to show opposite delta at 0.75 strength
3. BUT also require that we see at least ONE more bar in the next 2 bars that
   continues the momentum (same sign delta, not necessarily as strong)
4. This creates a "momentum cascade" requirement - not just one reversal bar,
   but sustained momentum
5. Keep all proven exit parameters: stop_offset=0.40, target_offset=0.65,
   max_hold_bars=6

Expected outcome: 20-35 trades with score around -0.1100 to -0.1400
- Even more selective than #39
- Filters out one-bar reversals that don't persist
- Requires actual momentum shift, not just a single bar
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
        initiation_window: int = 5,
        initiation_delta_strength: float = 0.75,
        prior_delta_threshold: float = 32.0,
        close_quartile_threshold: float = 0.33,
        momentum_confirmation_bars: int = 2,
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
                # Check if this bar shows initiation
                if self._is_initiation_bar(fast_bar, self.absorption_signal):
                    self.initiation_bar_count += 1
                    
                    # Check if we have enough momentum confirmation bars
                    if self.initiation_bar_count >= self.momentum_confirmation_bars:
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
        Check if bar shows initiation: opposite delta flow.
        For buy absorption (we want to sell): need negative delta
        For sell absorption (we want to buy): need positive delta
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
