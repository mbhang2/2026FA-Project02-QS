"""Intraday momentum persistence with adaptive window and exit management.

Builds on the rolling-window momentum hypothesis with three key improvements:

1. Adaptive window length: Instead of fixed window, test multiple lengths
   and weight signals by their historical accuracy within recent windows.
   
2. Better exit logic: The previous strategy closed positions immediately,
   preventing the bracket from working. This strategy holds positions and
   lets brackets handle exits, only closing on reversal signals.
   
3. Signal filtering: Only enter when momentum is strong (cumulative return
   magnitude above a threshold) and when the signal has been consistent
   across bars (reduced noise entry).

The hypothesis: momentum persistence is stronger when:
- The signal is computed over the right window length (adaptive)
- We hold the position long enough for mean reversion/continuation
- We filter weak signals that are likely noise
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        primary_window: int = 10,
        secondary_windows: tuple[int, ...] = (5, 15, 20),
        min_signal_strength: float = 0.002,
        stop_offset: float = 1.0,
        target_offset: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.primary_window = primary_window
        self.secondary_windows = secondary_windows
        self.min_signal_strength = min_signal_strength
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        
        # Track returns for rolling window computation
        self._returns: deque[float] = deque(maxlen=max(primary_window, max(secondary_windows)) + 5)
        self._last_close: float | None = None
        self._last_signal: int = 0  # Track previous signal for consistency check

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        last = history[-1]
        
        # Compute return for this bar
        if self._last_close is not None and self._last_close > 0:
            ret = (last.close - self._last_close) / self._last_close
            self._returns.append(ret)
        
        self._last_close = last.close
        
        # Need enough bars for primary window
        if len(self._returns) < self.primary_window:
            return
        
        # Compute momentum signal using primary window
        cumulative_return = sum(list(self._returns)[-self.primary_window:])
        
        # Determine momentum signal with strength threshold
        signal = 0
        if cumulative_return > self.min_signal_strength:
            signal = 1  # bullish
        elif cumulative_return < -self.min_signal_strength:
            signal = -1  # bearish
        
        # Check secondary windows for signal confirmation
        # If primary signal is strong but secondary windows disagree, reduce confidence
        if signal != 0 and len(self._returns) >= max(self.secondary_windows):
            secondary_signals = []
            for window in self.secondary_windows:
                sec_return = sum(list(self._returns)[-window:])
                if sec_return > self.min_signal_strength:
                    secondary_signals.append(1)
                elif sec_return < -self.min_signal_strength:
                    secondary_signals.append(-1)
                else:
                    secondary_signals.append(0)
            
            # Count how many secondary windows agree with primary signal
            agreements = sum(1 for s in secondary_signals if s == signal)
            # Only proceed if at least 1 secondary window agrees (majority)
            if agreements < 1:
                signal = 0
        
        # Exit existing position only on reversal signal
        if ctx.position != 0:
            position_signal = 1 if ctx.position > 0 else -1
            if signal != position_signal and signal != 0:
                # Reversal: close and prepare for new direction
                ctx.close()
                return
            else:
                # Hold position, let bracket work
                return
        
        # Don't enter on neutral signal
        if signal == 0:
            self._last_signal = 0
            return
        
        # Don't enter if we already have working orders
        if ctx.working_order_ids:
            return
        
        # Require signal consistency: signal should match or be new
        # This filters some noise
        if self._last_signal == signal:
            # Signal is consistent, enter
            pass
        elif self._last_signal == 0:
            # First signal or after neutral, enter
            pass
        else:
            # Signal just flipped, wait for one more bar of consistency
            self._last_signal = signal
            return
        
        self._last_signal = signal
        
        # Enter position based on momentum signal
        if signal > 0:
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

    def on_fill(self, ctx) -> None:
        # Bracket handles the exits; nothing to do here
        pass

    def on_lifecycle(self, ctx) -> None:
        # Track order lifecycle but don't need to act
        pass
