"""A momentum strategy with an ATR-scaled entry threshold and bracket.

The previous best required a bar body of at least 0.25 * ATR. That left some
moderate but still directional bars untraded. This version keeps the same
confirmation logic -- price body agrees with signed flow -- but accepts bodies
of at least 0.20 * ATR. All other mechanics are unchanged: ATR is computed from
the prior closed bars only, and the bracket distances are scaled by the same
ATR so the exit adapts to current volatility.

Interval remains 1m: it gives enough closed bars per session for a rolling
volatility estimate without paying the hit of a tick subscription.
"""

from __future__ import annotations


class Strategy:
    """Momentum filtered by body size relative to ATR, with a lower threshold."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        lookback: int = 8,
        entry_frac: float = 0.20,
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

        # Lookback + 1 closed bars: prior window for ATR, last bar for signal.
        recent = ctx.history(self.interval).last(self.lookback + 1)
        if len(recent) < self.lookback + 1:
            return

        prior = recent[:-1]
        if len(prior) < 2:
            return

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
            return

        atr = sum(trs) / len(trs)
        if atr <= 0:
            return

        last = recent[-1]
        body = last.close - last.open
        threshold = self.entry_frac * atr

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        # Delta confirms the price move; a contradictory delta vetoes the signal.
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
        pass
