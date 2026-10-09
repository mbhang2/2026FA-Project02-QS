"""Intraday momentum persistence strategy using rolling windows.

Tests the hypothesis that five-minute returns exhibit autocorrelation within
rolling windows, such that the sign and magnitude of recent cumulative returns
predict the direction of the next bar.

Strategy:
1. Compute rolling cumulative returns over a configurable window length (W bars)
2. Generate momentum signal as sign(cumulative_return)
3. Enter long/short at the open of the next bar if signal is non-zero
4. Exit after one bar holding period (at the close of the next bar)
5. Use brackets for protection from the instant of entry

The strategy tests multiple window lengths to find which persistence window
best captures intraday momentum effects on this instrument.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        window_length: int = 10,
        stop_offset: float = 0.75,
        target_offset: float = 0.75,
        size: int = 1,
    ):
        self.interval = interval
        self.window_length = window_length
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        
        # Track returns for rolling window computation
        self._returns: list[float] = []
        self._bar_count = 0
        self._last_close: float | None = None

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
        if self._last_close is not None and last.open > 0:
            ret = (last.close - self._last_close) / self._last_close
            self._returns.append(ret)
            # Keep only what we need for the window
            if len(self._returns) > self.window_length:
                self._returns.pop(0)
        
        self._last_close = last.close
        self._bar_count += 1
        
        # Need at least window_length bars to compute momentum
        if len(self._returns) < self.window_length:
            return
        
        # Compute cumulative return over the window
        cumulative_return = sum(self._returns[-self.window_length:])
        
        # Determine momentum signal
        if cumulative_return > 0:
            signal = 1  # bullish
        elif cumulative_return < 0:
            signal = -1  # bearish
        else:
            signal = 0  # neutral
        
        # Exit existing position before entering new one
        if ctx.position != 0:
            ctx.close()
            return
        
        # Don't enter on neutral signal
        if signal == 0:
            return
        
        # Don't enter if we already have working orders
        if ctx.working_order_ids:
            return
        
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
        # Position management happens in on_bars via close()
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
