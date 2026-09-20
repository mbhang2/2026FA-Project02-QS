"""
Absorption and initiation strategy for delta-based order flow.

The hypothesis: absorption (large delta with price disagreement) marks a turn.
Entry comes when we see initiation in the opposite direction (delta-efficient
move). This strategy tests the core claim on 1-minute bars.

Key design choices:
1. Use 1-minute bars for delta measurement (absorption is intra-bar flow)
2. Measure delta against recent session range to normalize for time-of-day
3. Absorption = large delta that fails to extend prior extremes
4. Initiation = delta-efficient move in opposite direction within next 2 bars
5. Entry only when actionable and flat, with bracket protection
6. Commission awareness: tight stops and targets to survive costs

The strategy tracks:
- Recent delta range (last 20 bars) for normalization
- Absorption signals (large delta with price rejection)
- Initiation confirmation (efficient opposite flow within 2 bars)
"""

from collections import deque
from typing import Deque, Optional


class Strategy:
    def __init__(
        self,
        interval: str = "1m",
        lookback: int = 20,
        delta_threshold: float = 1.5,
        stop_offset: float = 0.5,
        target_offset: float = 0.75,
        size: int = 1,
        max_bars_to_initiation: int = 2,
    ) -> None:
        self.interval: str = interval
        self.lookback: int = lookback
        self.delta_threshold: float = delta_threshold
        self.stop_offset: float = stop_offset
        self.target_offset: float = target_offset
        self.size: int = size
        self.max_bars_to_initiation: int = max_bars_to_initiation

        # Track state
        self.delta_history: Deque[int] = deque(maxlen=lookback)
        self.bar_history: Deque = deque(maxlen=max_bars_to_initiation + 1)
        self.absorption_signal: Optional[str] = None  # 'buy_absorption' or 'sell_absorption'
        self.bars_since_absorption: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Get the bar that just closed
        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last_bar = history[-1]

        # Update our rolling history
        self.bar_history.append(last_bar)
        self.delta_history.append(last_bar.delta)

        # Check if we have an open position or working order
        if ctx.position or ctx.working_order_ids:
            # Track bars since absorption for initiation detection
            if self.absorption_signal is not None:
                self.bars_since_absorption += 1
            return

        # Detect absorption: large delta that fails to extend price
        absorption = self._detect_absorption(history)

        if absorption:
            # Set absorption signal for next bars to confirm with initiation
            self.absorption_signal = absorption
            self.bars_since_absorption = 0
            return

        # Check for initiation after absorption
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            if self.bars_since_absorption <= self.max_bars_to_initiation:
                initiation = self._detect_initiation(self.absorption_signal)
                if initiation:
                    # Place entry order
                    self._place_entry(ctx, self.absorption_signal)
                    self.absorption_signal = None
                    self.bars_since_absorption = 0
            else:
                # Too many bars since absorption, reset
                self.absorption_signal = None
                self.bars_since_absorption = 0

    def _detect_absorption(self, history) -> Optional[str]:
        """
        Detect absorption: large delta that fails to extend price.
        Returns 'buy_absorption' or 'sell_absorption' if detected, else None.
        """
        if len(history) < 2:
            return None

        last = history[-1]
        prior = history[-2]

        # Need enough delta history to compute threshold
        if len(self.delta_history) < self.lookback // 2:
            return None

        # Compute normalized delta threshold
        abs_deltas = [abs(d) for d in self.delta_history]
        avg_abs_delta = sum(abs_deltas) / len(abs_deltas) if abs_deltas else 1.0
        threshold = max(avg_abs_delta * self.delta_threshold, 50)

        # Buy absorption: large positive delta but price fails
        if last.delta > threshold:
            # Price fails if it doesn't exceed prior high or closes below it
            if last.high <= prior.high or last.close < prior.close:
                return "buy_absorption"

        # Sell absorption: large negative delta but price fails
        if last.delta < -threshold:
            # Price fails if it doesn't go below prior low or closes above it
            if last.low >= prior.low or last.close > prior.close:
                return "sell_absorption"

        return None

    def _detect_initiation(self, absorption_signal: str) -> bool:
        """
        Detect initiation: delta-efficient move in opposite direction.
        Initiation = large opposite delta with price moving in delta direction.
        """
        if len(self.bar_history) < 2:
            return False

        last = self.bar_history[-1]

        # Compute normalized threshold
        if len(self.delta_history) < self.lookback // 2:
            return False

        abs_deltas = [abs(d) for d in self.delta_history]
        avg_abs_delta = sum(abs_deltas) / len(abs_deltas) if abs_deltas else 1.0
        threshold = max(avg_abs_delta * self.delta_threshold, 50)

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: large negative delta + closes near low
            if last.delta < -threshold:
                # Price agrees with delta: close near low
                bar_range = last.high - last.low
                if bar_range > 0:
                    close_to_low = (last.high - last.close) / bar_range
                    if close_to_low > 0.4:  # Close in upper 40% of range = near low
                        return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: large positive delta + closes near high
            if last.delta > threshold:
                # Price agrees with delta: close near high
                bar_range = last.high - last.low
                if bar_range > 0:
                    close_to_high = (last.close - last.low) / bar_range
                    if close_to_high > 0.4:  # Close in upper 40% of range = near high
                        return True

        return False

    def _place_entry(self, ctx, absorption_signal: str) -> None:
        """Place entry order based on absorption signal."""
        if absorption_signal == "buy_absorption":
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-buy",
            )
        elif absorption_signal == "sell_absorption":
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-sell",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
