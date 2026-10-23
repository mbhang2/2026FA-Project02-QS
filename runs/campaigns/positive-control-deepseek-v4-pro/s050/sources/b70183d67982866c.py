"""Next strategy: keep the delta-confirmed 5m momentum core, but make the
protection adaptive and add a trend filter.

The prior run used a single bar's high-low range for the bracket. That is noisy:
one isolated wide bar widens the stop and target for a trade that was triggered
by a normal bar, and one quiet bar tightens them too much. This version smooths
the volatility estimate with an average true range over a short lookback and
requires the bar's close to be on the right side of a short moving average,
which removes counter-trend entries.
"""

from __future__ import annotations


class Strategy:
    """Delta-confirmed momentum with an ATR-scaled bracket and a trend filter."""

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
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        # A window of closed bars including the one that just closed.
        # `last()` is bounded by what exists, so the length check above is
        # the warmup gate.
        window = history.last(self.lookback + 1)
        last = window[-1]
        prior = window[:-1]

        # Trend filter: current close relative to the prior bars' average close.
        trend = sum(b.close for b in prior) / self.lookback

        # Average true range over the same window, smoothing the protection
        # instead of using a single bar's high-low range.
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
        if last.close > last.open and delta > 0 and last.close > trend:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="trend-long",
            )
        elif last.close < last.open and delta < 0 and last.close < trend:
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
