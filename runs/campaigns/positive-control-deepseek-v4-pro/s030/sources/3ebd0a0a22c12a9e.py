"""Breakout momentum: trade only when the last bar closes beyond the prior N-bar range.

This extends the previous ATR-filtered momentum strategy by requiring an actual
breakout of the prior bar window instead of merely a larger-than-average body.
The breakout criterion removes bars that moved hard but stayed inside a range,
which are more likely to reverse. Delta still confirms directional agreement,
and the bracket remains ATR-scaled so it adapts to current volatility.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        lookback: int = 6,
        entry_frac: float = 0.10,
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
        last = recent[-1]
        if len(prior) < 2:
            return

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

        atr = sum(trs) / len(trs)
        if atr <= 0:
            return

        prior_high = max(b.high for b in prior)
        prior_low = min(b.low for b in prior)

        body = last.close - last.open
        threshold = self.entry_frac * atr

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        if last.close > prior_high and body >= threshold and last.delta >= 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="breakout-long",
            )
        elif last.close < prior_low and body <= -threshold and last.delta <= 0:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="breakout-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
