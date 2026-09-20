"""
Absorption and initiation strategy with refined entry logic and position management.

Prior trials scored -0.6165, -0.3989, -0.3583, and -0.2164. The best trial (#3)
improved significantly by loosening the multi-timeframe filter and extending hold
periods. However, analysis suggests several issues:

1. Entry signal timing: Current logic waits for initiation within 2 bars of
   absorption. But initiation is the reversal signal itself - we may be entering
   too late or missing the best entry price.

2. Entry direction logic: Trial #3 appears to have the entry directions reversed.
   Buy absorption (buyers failed) should lead to SHORT entry (selling), but the
   code places a sell() order. This is confusing and may be working against itself.

3. Position sizing and risk: At 0.40 stop and 0.65 target, we're risking 0.40 to
   make 0.65 (1.625:1 ratio). With commission at 0.1 per side (0.2 round trip),
   effective target becomes 0.45, ratio becomes 1.125:1. This is tight.

4. Absorption detection strictness: Requiring both "fails to extend high/low" AND
   "closes opposite prior close" may be too strict. We may be missing valid
   absorption bars.

5. Initiation detection: Requiring close in upper/lower 50% of range is reasonable
   but may miss some valid initiations.

Refinements for this iteration:

1. Clarify entry logic: Buy absorption (buyers rejected) = SELL entry (short).
   Sell absorption (sellers rejected) = BUY entry (long). Make this explicit.

2. Adjust absorption detection: Keep the "fails to extend" check but relax the
   close requirement. A bar that shows large delta but fails to extend extremes
   is absorption regardless of where it closes relative to prior bar.

3. Faster entry on initiation: Place entry immediately on initiation detection,
   not on the next bar. This captures the reversal momentum.

4. Improve stop/target sizing: Use 0.50 stop, 0.75 target to give more room
   (1.5:1 ratio before commission). This reduces whipsaws from tight stops.

5. Add a secondary filter: Only trade absorption if the prior bar's delta was
   in the same direction. This confirms we're seeing a real reversal pattern,
   not just noise.

6. Extended lookback for delta threshold: Increase to 30 bars to get better
   percentile statistics on what "large" delta means.

7. Reduce early-exit aggression: Keep max_hold at 10 bars but only exit on loss
   if we've held 12+ bars (give it more time to work).
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
        stop_offset: float = 0.50,
        target_offset: float = 0.75,
        size: int = 1,
        max_hold_bars: int = 10,
        exit_loss_after_bars: int = 12,
        initiation_window: int = 2,
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
        self.exit_loss_after_bars: int = exit_loss_after_bars
        self.initiation_window: int = initiation_window

        # Fast (1m) history
        self.fast_delta_history: Deque[int] = deque(maxlen=lookback)
        self.fast_volume_history: Deque[int] = deque(maxlen=lookback)
        self.fast_high_history: Deque[float] = deque(maxlen=lookback)
        self.fast_low_history: Deque[float] = deque(maxlen=lookback)
        self.fast_close_history: Deque[float] = deque(maxlen=lookback)

        # Signal tracking
        self.absorption_signal: Optional[str] = None
        self.absorption_delta: int = 0
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
            # Exit on sustained loss after sufficient hold time
            if self.bars_held >= self.exit_loss_after_bars and ctx.unrealized_pnl < 0:
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
            self.absorption_delta = fast_bar.delta
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
        make a new high relative to prior bar.
        
        Sell absorption: Large negative delta (sellers aggressive) but fails to
        make a new low relative to prior bar.
        
        Secondary filter: Prior bar's delta should be in same direction, confirming
        the reversal pattern (trend followed by failed continuation).
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
            # Must fail to make new high
            if last.high <= prior.high:
                # Prior bar should show positive delta (trend continuation attempt)
                if prior.delta > 0:
                    return "buy_absorption"

        # Sell absorption: large negative delta but fails to extend low
        if last.delta < -threshold:
            # Must fail to make new low
            if last.low >= prior.low:
                # Prior bar should show negative delta (trend continuation attempt)
                if prior.delta < 0:
                    return "sell_absorption"

        return None

    def _detect_initiation(self, bar, absorption_signal: str) -> bool:
        """
        Detect initiation: opposite delta flow that efficiently moves price.
        
        Initiation must:
        1. Have opposite delta from absorption signal
        2. Exceed the threshold
        3. Move price in the direction of the delta
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta + closes low
            if bar.delta < -threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0:
                    # Close in lower half of range (price moved down with selling)
                    close_to_low = (bar.high - bar.close) / bar_range
                    if close_to_low > 0.5:
                        return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta + closes high
            if bar.delta > threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0:
                    # Close in upper half of range (price moved up with buying)
                    close_to_high = (bar.close - bar.low) / bar_range
                    if close_to_high > 0.5:
                        return True

        return False

    def _place_entry(self, ctx, absorption_signal: str) -> None:
        """
        Place entry order based on absorption signal.
        
        Buy absorption (buyers rejected) = SELL entry (trade the reversal down)
        Sell absorption (sellers rejected) = BUY entry (trade the reversal up)
        """
        if absorption_signal == "buy_absorption":
            # Buyers were absorbed/rejected, so we go short
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-short",
            )
        elif absorption_signal == "sell_absorption":
            # Sellers were absorbed/rejected, so we go long
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
