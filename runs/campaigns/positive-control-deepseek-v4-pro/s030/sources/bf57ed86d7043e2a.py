"""A dual-timeframe momentum strategy building on the best prior run (#7).

Run #7 used 1m bars, an 8-bar ATR estimate, a body threshold of 0.20 * ATR,
signed order-flow confirmation, a 12-bar 1m trend filter, and a late-session
gate. This version keeps that entire proven structure and adds a slow 5m trend
filter.

The idea: #7 only required the trigger bar to close above a short-term 1m
average. A move that is with the 1m trend but against the slower 5m trend is
more likely to be a counter-trend retracement that stalls. Requiring the most
recent completed 5m bar to close above its own short-term 5m average when the
slow history is long enough, or at least to be an up bar while it is still
forming, removes those trades while preserving the best with-trend entries.

The bracket is still armed by the engine at the fill, so the position is
protected from the instant it fills rather than from the next callback.
"""

from __future__ import annotations


class Strategy:
    """Momentum with ATR, delta, fast trend, and slow-timeframe trend gate."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        slow_interval: str = "5m",
        lookback: int = 8,
        trend_window: int = 12,
        slow_window: int = 4,
        entry_frac: float = 0.20,
        stop_mult: float = 1.5,
        target_mult: float = 3.0,
        size: int = 1,
        session_margin_frac: float = 0.10,
    ):
        self.interval = interval
        self.slow_interval = slow_interval
        self.lookback = lookback
        self.trend_window = trend_window
        self.slow_window = slow_window
        self.entry_frac = entry_frac
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.session_margin_frac = session_margin_frac

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        # Only act when a fast bar closes. The slow bar closing is a filter
        # update, not a decision point.
        if self.interval not in ctx.bars:
            return
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        # Do not open a fresh position in the last part of a session.
        session_len = ctx.session_end - ctx.session_start
        if session_len > 0:
            remaining = ctx.session_end - ctx.ts
            if remaining < session_len * self.session_margin_frac:
                return

        fast_hist = ctx.history(self.interval)
        if len(fast_hist) < max(self.lookback + 1, self.trend_window + 1):
            return

        # ATR over the bars immediately preceding the trigger bar.
        recent = fast_hist.last(self.lookback + 1)
        prior = recent[:-1]
        if len(prior) < 2:
            return

        trs = []
        prev_close = prior[0].close
        for bar in prior[1:]:
            trs.append(
                max(
                    bar.high - bar.low,
                    abs(bar.high - prev_close),
                    abs(bar.low - prev_close),
                )
            )
            prev_close = bar.close

        if not trs:
            return

        atr = sum(trs) / len(trs)
        if atr <= 0:
            return

        last = recent[-1]
        body = last.close - last.open
        threshold = self.entry_frac * atr

        # Fast trend alignment: close above/below the average of the previous
        # trend_window closes.
        prior_trend = fast_hist.last(self.trend_window + 1)[:-1]
        if len(prior_trend) < self.trend_window:
            return
        trend_mean = sum(b.close for b in prior_trend) / self.trend_window
        fast_uptrend = last.close > trend_mean
        fast_downtrend = last.close < trend_mean

        # Slow-timeframe trend filter.
        slow_hist = ctx.history(self.slow_interval)
        if len(slow_hist) < 1:
            return

        if len(slow_hist) >= self.slow_window:
            slow_bars = slow_hist.last(self.slow_window)
            if len(slow_bars) < self.slow_window:
                return
            slow_mean = sum(b.close for b in slow_bars) / self.slow_window
            slow_uptrend = slow_bars[-1].close > slow_mean
            slow_downtrend = slow_bars[-1].close < slow_mean
        else:
            slow_uptrend = slow_hist[-1].close > slow_hist[-1].open
            slow_downtrend = slow_hist[-1].close < slow_hist[-1].open

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        # Everything must agree: price direction, order flow, fast trend, and
        # slow trend.
        if (
            body >= threshold
            and last.delta >= 0
            and fast_uptrend
            and slow_uptrend
        ):
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-long",
            )
        elif (
            body <= -threshold
            and last.delta <= 0
            and fast_downtrend
            and slow_downtrend
        ):
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
