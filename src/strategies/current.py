"""Intraday momentum persistence with mean-reversion confirmation.

The previous strategies suffered from:
1. Too frequent entries - entering on every bar with momentum signal
2. Weak signal confirmation - minimal filtering for noise
3. Poor exit timing - either closing too early or holding too long

This strategy improves by:
1. Using momentum acceleration (change in momentum) as a filter
2. Requiring multi-bar signal confirmation before entry
3. Implementing a holding period that lets the bracket work
4. Tracking rolling accuracy to avoid entering when signal is unreliable

The hypothesis: momentum persistence is stronger when we:
- Confirm the signal is accelerating (not just present)
- Wait for 2+ consecutive bars of agreement
- Let positions develop rather than exiting immediately
- Avoid trading when recent accuracy is poor
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        window_length: int = 10,
        confirmation_bars: int = 2,
        min_momentum_magnitude: float = 0.001,
        stop_offset: float = 1.0,
        target_offset: float = 1.5,
        size: int = 1,
        min_accuracy_threshold: float = 0.48,
        accuracy_window: int = 50,
    ):
        self.interval = interval
        self.window_length = window_length
        self.confirmation_bars = confirmation_bars
        self.min_momentum_magnitude = min_momentum_magnitude
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_accuracy_threshold = min_accuracy_threshold
        self.accuracy_window = accuracy_window
        
        # Track returns for rolling window computation
        self._returns: deque[float] = deque(maxlen=max(window_length, accuracy_window) + 10)
        self._last_close: float | None = None
        
        # Track signal confirmation
        self._signal_history: deque[int] = deque(maxlen=confirmation_bars + 1)
        self._bars_since_entry: int = 0
        
        # Track prediction accuracy for signal reliability
        self._predictions: deque[tuple[int, int]] = deque(maxlen=accuracy_window)
        # Each tuple: (predicted_signal, actual_next_bar_direction)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 2:
            return
        
        last = history[-1]
        prev = history[-2] if len(history) >= 2 else None
        
        # Compute return for this bar
        if self._last_close is not None and self._last_close > 0:
            ret = (last.close - self._last_close) / self._last_close
            self._returns.append(ret)
        
        self._last_close = last.close
        
        # Track actual direction for accuracy measurement
        if prev is not None and len(self._signal_history) > 0:
            actual_direction = 1 if last.close > prev.close else (-1 if last.close < prev.close else 0)
            last_predicted = self._signal_history[-1] if self._signal_history else 0
            if last_predicted != 0:
                self._predictions.append((last_predicted, actual_direction))
        
        # Need enough bars for window
        if len(self._returns) < self.window_length:
            return
        
        # Compute current momentum
        returns_list = list(self._returns)
        cumulative_return = sum(returns_list[-self.window_length:])
        
        # Determine momentum signal
        if cumulative_return > self.min_momentum_magnitude:
            current_signal = 1  # bullish
        elif cumulative_return < -self.min_momentum_magnitude:
            current_signal = -1  # bearish
        else:
            current_signal = 0  # neutral
        
        self._signal_history.append(current_signal)
        
        # Calculate signal reliability (accuracy of recent predictions)
        signal_accuracy = self._calculate_accuracy()
        
        # If signal reliability is too low, don't trade
        if signal_accuracy < self.min_accuracy_threshold:
            # Close position if we have one and accuracy dropped
            if ctx.position != 0:
                ctx.close()
            return
        
        # Check if we have a confirmed signal (multiple bars of agreement)
        confirmed_signal = self._get_confirmed_signal()
        
        # Manage existing position
        if ctx.position != 0:
            self._bars_since_entry += 1
            position_signal = 1 if ctx.position > 0 else -1
            
            # Close on reversal signal
            if confirmed_signal != 0 and confirmed_signal != position_signal:
                ctx.close()
                self._bars_since_entry = 0
            # Also close after holding for a reasonable time to let bracket work
            elif self._bars_since_entry > 3:
                # Let the bracket handle exit, just check if position still exists
                pass
            return
        
        # Don't enter if we already have working orders
        if ctx.working_order_ids:
            return
        
        # Only enter on confirmed signal
        if confirmed_signal == 0:
            return
        
        # Additional filter: check momentum acceleration
        if len(returns_list) >= self.window_length + 1:
            prev_cumulative = sum(returns_list[-(self.window_length + 1):-1])
            momentum_acceleration = cumulative_return - prev_cumulative
            
            # Only enter if momentum is accelerating in the signal direction
            if confirmed_signal > 0 and momentum_acceleration < 0:
                return
            if confirmed_signal < 0 and momentum_acceleration > 0:
                return
        
        # Enter position based on confirmed momentum signal
        self._bars_since_entry = 0
        if confirmed_signal > 0:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="momentum-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="momentum-short",
            )

    def _get_confirmed_signal(self) -> int:
        """Return signal only if confirmed by multiple consecutive bars."""
        if len(self._signal_history) < self.confirmation_bars:
            return 0
        
        recent = list(self._signal_history)[-self.confirmation_bars:]
        
        # All recent signals must agree and be non-zero
        if all(s == recent[0] for s in recent) and recent[0] != 0:
            return recent[0]
        
        return 0

    def _calculate_accuracy(self) -> float:
        """Calculate recent prediction accuracy."""
        if len(self._predictions) == 0:
            return 0.5  # Default to neutral
        
        correct = sum(
            1 for pred_signal, actual_direction in self._predictions
            if (pred_signal > 0 and actual_direction > 0) or 
               (pred_signal < 0 and actual_direction < 0)
        )
        
        return correct / len(self._predictions)

    def on_fill(self, ctx) -> None:
        # Bracket handles the exits
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
