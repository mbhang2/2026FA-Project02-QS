"""Next strategy: keep the #5 core (three-bar breakout) and add cumulative
delta confirmation over the breakout window.

Prior trials:
#0 simple close-to-close momentum: 1,241 trades, +3.3002
#1 ATR-scaled + short trend filter: 895 trades, +4.0299
#2 adding 20-bar range + volume filter: 527 trades, +3.0401
#3 one-bar breakout: 846 trades, +4.6289
#4 two-bar breakout: 830 trades, +4.8108
#5 three-bar breakout: 813 trades, +4.9760
#6 five-bar breakout: 770 trades, +4.7048

Widening the breakout filter from three to five bars reduced both trade count
and total score, while per-trade quality was roughly flat. The better next step
is to stay at three bars and remove marginal signals with a different
confirmation: require the same cumulative order-flow sign across the breakout
window, not just on the signal bar. This keeps the immediate expansion signal
while filtering breakouts that are not supported by recent delta.
"""

from __future__ import annotations


class Strategy:
    """Delta-confirmed three-bar breakout with ATR brackets."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 20,
        breakout_bars: int = 3,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.breakout_bars = breakout_bars
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

        window = history.last(self.lookback + 1)
        if len(window) < self.lookback + 1:
            return
        if len(window) < self.breakout_bars + 1:
            return

        last = window[-1]
        prior = window[:-1]

        # Trend anchor: average close over the lookback window before the
        # signal bar.
        trend = sum(b.close for b in prior) / self.lookback

        # ATR over the same window, smoothing the bracket size.
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

        # The prior `breakout_bars` bars define the immediate range to clear.
        recent = window[-self.breakout_bars - 1:-1]
        recent_high = max(b.high for b in recent)
        recent_low = min(b.low for b in recent)

        last_delta = last.delta
        recent_delta = sum(b.delta for b in recent)

        if (
            last.close > last.open
            and last_delta > 0
            and recent_delta > 0
            and last.close > trend
            and last.close > recent_high
        ):
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="trend-long",
            )
        elif (
            last.close < last.open
            and last_delta < 0
            and recent_delta < 0
            and last.close < trend
            and last.close < recent_low
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
