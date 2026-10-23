"""A 1m momentum strategy with ATR-scaled entry and bracket, plus session-close flattening.

The prior best used 1m bars, lookback 8, stop 1.5 ATR, target 2.5 ATR.
This version shortens the ATR lookback to 6 so the volatility estimate reacts
faster, and adds explicit flattening at the session close. The session-close
logic cancels any remaining bracket legs and queues a GTC market close, so an
open position is not carried across the simulated session boundary overnight.
"""
from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        lookback: int = 6,
        entry_frac: float = 0.25,
        stop_mult: float = 1.5,
        target_mult: float = 2.5,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.entry_frac = entry_frac
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _atr(self, ctx):
        recent = ctx.history(self.interval).last(self.lookback + 1)
        if len(recent) < self.lookback + 1:
            return None

        prior = recent[:-1]
        if len(prior) < 2:
            return None

        trs = []
        for i in range(1, len(prior)):
            prev_close = prior[i - 1].close
            high_i = prior[i].high
            low_i = prior[i].low
            trs.append(
                max(
                    high_i - low_i,
                    abs(high_i - prev_close),
                    abs(low_i - prev_close),
                )
            )

        if not trs:
            return None

        return sum(trs) / len(trs)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        atr = self._atr(ctx)
        if atr is None or atr <= 0:
            return

        last = ctx.history(self.interval)[-1]
        body = last.close - last.open
        threshold = self.entry_frac * atr

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        # Price body and signed flow must agree.
        if body >= threshold and last.delta >= 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-long",
            )
        elif body <= -threshold and last.delta <= 0:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind != "session_close":
            return

        # Do not carry a simulated position across the session boundary.
        if not ctx.actionable or not ctx.position:
            return

        for order_id in ctx.working_order_ids:
            ctx.cancel(order_id)

        ctx.close(tif="GTC", tag="session-close")
