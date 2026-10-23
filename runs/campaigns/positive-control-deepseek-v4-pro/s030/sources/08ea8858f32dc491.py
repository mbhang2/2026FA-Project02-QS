"""A momentum strategy with a volatility-scaled entry threshold and bracket.

The baseline buys or sells on any up/down close. This version stays close to
that planted signal but only acts when the last closed bar's body is large
relative to a rolling average true range, and its delta does not disagree with
the price move. The bracket distances are also scaled by ATR rather than fixed,
so the exit is sized to current volatility instead of a constant 0.75 points.

Interval is 1m rather than 5m: the simulated run has only 390 ticks per session,
and bar intervals are cheap while still giving enough closed bars for a rolling
volatility estimate.
"""

from __future__ import annotations


class Strategy:
    """Momentum filtered by body size relative to ATR, with ATR-scaled bracket."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        lookback: int = 8,
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

        # Lookback + 1 closed bars: prior window for ATR, last bar for signal.
        recent = ctx.history(self.interval).last(self.lookback + 1)
        if len(recent) < self.lookback + 1:
            return

        prior = recent[:-1]
        trs = []
        for i in range(1, len(prior)):
            prev_close = prior[i - 1].close
            high = prior[i].high
            low = prior[i].low
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

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
