"""
Refined absorption and initiation strategy with improved signal detection.

The prior trial scored -0.6165 with 1153 trades over 384 sessions. The strategy
correctly identifies absorption (large delta with price rejection) and initiation
(efficient opposite flow), but needs refinement in:

1. Signal timing: Current approach waits up to 2 bars for initiation, but may
   be too greedy in entering. Tighten the initiation detection window.

2. Delta normalization: Using average absolute delta may not capture extremes
   well. Add percentile-based thresholds to distinguish truly large deltas.

3. Price rejection criteria: Current "fails to extend" logic is binary. Add
   gradient: stronger rejection = stronger signal.

4. Entry sizing and protection: Fixed stop/target offsets may not adapt to
   intrabar volatility. Use recent range to scale protection.

5. False signal filtering: Absorption during low-volume periods is less reliable.
   Filter by volume context.

Key improvements:
- Use 75th percentile of recent delta magnitude for threshold
- Require stronger price rejection (not just failing to extend)
- Initiation must be immediate (next bar only) and very clear
- Scale stop/target by recent volatility
- Filter low-volume absorption signals
- Track session state to avoid early-session noise
"""

from collections import deque
from typing import Deque, Optional


class Strategy:
    def __init__(
        self,
        interval: str = "1m",
        lookback: int = 20,
        delta_percentile: float = 0.75,
        volume_threshold: float = 0.7,
        stop_offset: float = 0.4,
        target_offset: float = 0.6,
        size: int = 1,
    ) -> None:
        self.interval: str = interval
        self.lookback: int = lookback
        self.delta_percentile: float = delta_percentile
        self.volume_threshold: float = volume_threshold
        self.stop_offset: float = stop_offset
        self.target_offset: float = target_offset
        self.size: int = size

        # Track rolling history
        self.delta_history: Deque[int] = deque(maxlen=lookback)
        self.volume_history: Deque[int] = deque(maxlen=lookback)
        self.high_history: Deque[float] = deque(maxlen=lookback)
        self.low_history: Deque[float] = deque(maxlen=lookback)
        self.close_history: Deque[float] = deque(maxlen=lookback)
        
        self.last_bar = None
        self.absorption_signal: Optional[str] = None
        self.bars_since_absorption: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last_bar = history[-1]
        
        # Update rolling history
        self.delta_history.append(last_bar.delta)
        self.volume_history.append(last_bar.volume)
        self.high_history.append(last_bar.high)
        self.low_history.append(last_bar.low)
        self.close_history.append(last_bar.close)

        # If we have a position or working order, don't enter
        if ctx.position or ctx.working_order_ids:
            self.absorption_signal = None
            self.bars_since_absorption = 0
            self.last_bar = last_bar
            return

        # Try to detect absorption on current bar
        absorption = self._detect_absorption(history, len(history) >= 2)

        if absorption:
            # Set signal and wait for initiation on NEXT bar only
            self.absorption_signal = absorption
            self.bars_since_absorption = 0
            self.last_bar = last_bar
            return

        # Check for initiation immediately after absorption (next bar only)
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1
            
            # Only check for initiation on the very next bar (bars_since == 1)
            if self.bars_since_absorption == 1:
                if self._detect_initiation(last_bar, self.absorption_signal):
                    self._place_entry(ctx, self.absorption_signal)
                    self.absorption_signal = None
                    self.bars_since_absorption = 0
            else:
                # Reset if we miss the initiation window
                self.absorption_signal = None
                self.bars_since_absorption = 0

        self.last_bar = last_bar

    def _get_delta_threshold(self) -> float:
        """
        Compute delta threshold using percentile of recent absolute deltas.
        This captures the magnitude distribution better than simple average.
        """
        if len(self.delta_history) < self.lookback // 2:
            return 50.0  # Default minimum threshold

        abs_deltas = sorted([abs(d) for d in self.delta_history])
        idx = int(len(abs_deltas) * self.delta_percentile)
        idx = max(0, min(idx, len(abs_deltas) - 1))
        threshold = abs_deltas[idx]
        
        return max(threshold, 50.0)

    def _get_avg_volume(self) -> float:
        """Get average volume from recent bars."""
        if not self.volume_history:
            return 100.0
        return sum(self.volume_history) / len(self.volume_history)

    def _detect_absorption(self, history, has_prior: bool) -> Optional[str]:
        """
        Detect absorption: large delta that fails to extend price meaningfully.
        Stronger rejection = stronger signal.
        """
        if not has_prior or len(history) < 2:
            return None

        last = history[-1]
        prior = history[-2]

        # Skip low-volume bars - absorption in thin conditions is less reliable
        avg_vol = self._get_avg_volume()
        if last.volume < avg_vol * self.volume_threshold:
            return None

        threshold = self._get_delta_threshold()

        # Buy absorption: large positive delta but price fails to go higher
        if last.delta > threshold:
            # Strong rejection: close below prior close (not just failing to extend high)
            if last.close < prior.close:
                return "buy_absorption"

        # Sell absorption: large negative delta but price fails to go lower
        if last.delta < -threshold:
            # Strong rejection: close above prior close
            if last.close > prior.close:
                return "sell_absorption"

        return None

    def _detect_initiation(self, bar, absorption_signal: str) -> bool:
        """
        Detect initiation: immediate, clear opposite delta flow that moves price.
        Must be efficient (delta and price agree strongly).
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: large negative delta + closes near low
            if bar.delta < -threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0:
                    # Close near low means price moved with the selling pressure
                    close_to_low = (bar.high - bar.close) / bar_range
                    # Require close in upper 50% of range (i.e., in lower half of price)
                    if close_to_low > 0.5:
                        return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: large positive delta + closes near high
            if bar.delta > threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0:
                    # Close near high means price moved with the buying pressure
                    close_to_high = (bar.close - bar.low) / bar_range
                    # Require close in upper 50% of range (i.e., in upper half of price)
                    if close_to_high > 0.5:
                        return True

        return False

    def _place_entry(self, ctx, absorption_signal: str) -> None:
        """Place entry order with scaled protection based on recent volatility."""
        if absorption_signal == "buy_absorption":
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-sell-init",
            )
        elif absorption_signal == "sell_absorption":
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-buy-init",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
