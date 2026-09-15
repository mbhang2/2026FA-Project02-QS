"""Momentum strategy with end-of-day flattening.

Exploits the planted momentum signal by following the last closed bar's direction.
Uses a larger bracket to capture extended moves and explicitly flattens before
the session closes to avoid unmanaged overnight risk.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, interval: str = "5m", stop_offset: float = 1.5,
                 target_offset: float = 4.5, size: int = 1,
                 eod_minutes: int = 5):
        self.interval = interval
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.eod_minutes = eod_minutes

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        time_left = ctx.session_end - ctx.ts
        eod_ns = self.eod_minutes * 60 * 1_000_000_000

        # Flatten before the session ends to avoid unprotected overnight holds
        if time_left <= eod_ns:
            if ctx.position != 0:
                if not ctx.find_orders(tag="close_eod"):
                    ctx.cancel_all()
                    if ctx.position > 0:
                        ctx.sell(size=ctx.position, reduce_only=True, tag="close_eod")
                    elif ctx.position < 0:
                        ctx.buy(size=abs(ctx.position), reduce_only=True, tag="close_eod")
            return

        # Normal trading: one position at a time, rely on brackets for exits
        if ctx.position != 0 or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]
        
        if last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="entry_long"
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="entry_short"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
