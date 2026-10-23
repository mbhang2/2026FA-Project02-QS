"""Next strategy: keep the delta-confirmed, ATR-scaled 5m momentum core from
the best prior trial, but require the move to also make a new high/low against
the recent range and pass a volume filter.

The prior strategy fired whenever price closed above a simple average and the
bar delta agreed. Many of those closes occurred inside the established range --
fading moves rather than confirming them. Requiring a new session-to-date
extreme (high for longs, low for shorts) keeps entries aligned with genuine
range expansion, while the average-volume filter avoids acting on unusually
thin bars.
"""

from __future__ import annotations


class Strategy:
    """Delta-confirmed momentum with trend, new-extreme, and volume filters."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 20,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        min_volume_ratio: float = 0.75,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_volume_ratio = min_volume_ratio
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        window = history.last(self.lookback + 1)
        last = window[-1]
        prior = window[:-1]

        # Average true range over the lookback window. This smooths the bracket
        # size rather than letting one wide or quiet bar distort it.
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

        # Recent average close, used as the trend anchor.
        trend = sum(b.close for b in prior) / self.lookback

        # Volume filter: do not trade on unusually thin bars, which have noisy
        # delta and are more likely to be artifacts.
        avg_volume = sum(b.volume for b in prior) / self.lookback
        if avg_volume <= 0:
            return
        if last.volume < avg_volume * self.min_volume_ratio:
            return

        prior_high = max(b.high for b in prior)
        prior_low = min(b.low for b in prior)

        delta = last.delta

        if (
            last.close > last.open
            and last.close > trend
            and last.high > prior_high
            and delta > 0
        ):
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="trend-long",
            )
        elif (
            last.close < last.open
            and last.close < trend
            and last.low < prior_low
            and delta < 0
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
