"""Next strategy: keep the best prior core, but require the momentum close to
also break the *immediate previous bar's* high or low instead of a 20-bar
extreme.

Prior results:
- #0 simple close-to-close momentum: 1,241 trades, score +3.3002
- #1 adding ATR-scaled brackets + short trend filter: 895 trades, score +4.0299
- #2 adding a 20-bar new-extreme filter + a volume filter: 527 trades, score +3.0401

The lesson from #2 is that requiring a multi-bar range breakout removed too much
of the real signal. This version keeps the #1 delta-confirmed trend core but
adds the mildest expansion filter that still removes inside bars: the close must
clear the previous bar's high/low. That should skip some false momentum bars
without cutting trade count as sharply as the 20-bar extreme did.
"""

from __future__ import annotations


class Strategy:
    """Delta-confirmed momentum with ATR brackets and a one-bar breakout filter."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 20,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return  # one position at a time; the bracket handles exits

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        window = history.last(self.lookback + 1)
        last = window[-1]
        previous = window[-2]
        prior = window[:-1]

        # Short average close as a trend anchor.
        trend = sum(b.close for b in prior) / self.lookback

        # Average true range over the same window, smoothing the bracket rather
        # than letting one wide or quiet bar distort it.
        trs = []
        for i in range(1, len(window)):
            bar = window[i]
            prev_close = window[i - 1].close
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
            trs.append(tr)

        atr = sum(trs) / len(trs)
        if atr <= 0:
            return

        stop = atr * self.stop_mult
        target = atr * self.target_mult
        if stop <= 0 or target <= 0:
            return

        delta = last.delta

        if (
            last.close > last.open
            and delta > 0
            and last.close > trend
            and last.close > previous.high
        ):
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="trend-long",
            )
        elif (
            last.close < last.open
            and delta < 0
            and last.close < trend
            and last.close < previous.low
        ):
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="trend-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
