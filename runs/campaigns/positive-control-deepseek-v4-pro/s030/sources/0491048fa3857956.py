"""A 30-second momentum strategy with ATR-scaled filters and session-close flattening.

The previous best used 1-minute bars. This version moves to 30-second bars for
more opportunities while keeping the same volatility-scaled entry logic: it only
trades when the last closed bar's body is large relative to the prior bars' ATR
and the bar's signed order flow does not disagree with the price move.

The bracket distances remain ATR-scaled, so the stop and target adapt to the
shorter bar's lower absolute volatility. In addition, this strategy flattens any
open position at the session close instead of carrying it overnight. The close is
submitted as a GTC market order so it can fill on the first tick of the next
session even if no trade occurs at the exact session boundary.
"""

from __future__ import annotations


class Strategy:
    """Momentum on 30s bars, with ATR-scaled entry and bracket, plus EOD flattening."""

    def __init__(
        self,
        *,
        interval: str = "30s",
        lookback: int = 10,
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

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        recent = ctx.history(self.interval).last(self.lookback + 1)
        if len(recent) < self.lookback + 1:
            return

        prior = recent[:-1]

        trs = []
        for i in range(1, len(prior)):
            high_i = prior[i].high
            low_i = prior[i].low
            prev_close = prior[i - 1].close
            trs.append(
                max(
                    high_i - low_i,
                    abs(high_i - prev_close),
                    abs(low_i - prev_close),
                )
            )

        if not trs:
            return

        atr = sum(trs) / len(trs)
        if atr <= 0:
            return

        last = recent[-1]
        body = last.close - last.open
        threshold = self.entry_frac * atr

        stop = self.stop_mult * atr
        target = self.target_mult * atr

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

        # Carry no simulated position across a session boundary. Cancel any
        # remaining bracket legs first, then flatten with a GTC market order so
        # it can fill at the first eligible tick of the next session.
        if not ctx.actionable or not ctx.position:
            return

        for order_id in ctx.working_order_ids:
            ctx.cancel(order_id)

        ctx.close(tif="GTC", tag="session-close")
