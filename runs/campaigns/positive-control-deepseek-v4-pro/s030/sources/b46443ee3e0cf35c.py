"""Momentum with ATR, delta, fast trend, and a close-level breakout gate.

Run #7 was the best prior trial: 1m bars, an 8-bar ATR estimate, a body
threshold of 0.20*ATR, signed order-flow confirmation, a 12-bar fast trend
filter, and a late-session gate. Run #8 added a slow 5m trend filter and
scored worse, which suggests the useful signal is in the same 1m timeframe
and wider filters can remove good trades.

This version keeps #7's proven structure intact and adds one more same-
timeframe gate: for a long, the trigger bar must close above the highest
close of the previous trend_window bars; for a short, below the lowest.
It is a close-based Donchian breakout requiring the bar to make a new
local closing extreme, not just close above an average. The bracket is
still armed by the engine at the fill.
"""

from __future__ import annotations


class Strategy:
    """Momentum with ATR-scaled entries, delta confirmation, trend, and close breakout."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        lookback: int = 8,
        trend_window: int = 12,
        entry_frac: float = 0.20,
        stop_mult: float = 1.5,
        target_mult: float = 3.0,
        size: int = 1,
        session_margin_frac: float = 0.10,
    ):
        self.interval = interval
        self.lookback = lookback
        self.trend_window = trend_window
        self.entry_frac = entry_frac
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.session_margin_frac = session_margin_frac

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        # Do not open a fresh position in the last part of a session.
        session_len = ctx.session_end - ctx.session_start
        if session_len > 0:
            remaining = ctx.session_end - ctx.ts
            if remaining < session_len * self.session_margin_frac:
                return

        hist = ctx.history(self.interval)
        if len(hist) < max(self.lookback + 1, self.trend_window + 1):
            return

        # ATR over the bars immediately preceding the trigger bar.
        recent = hist.last(self.lookback + 1)
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
        prior_trend = hist.last(self.trend_window + 1)[:-1]
        if len(prior_trend) < self.trend_window:
            return
        trend_mean = sum(b.close for b in prior_trend) / self.trend_window
        fast_uptrend = last.close > trend_mean
        fast_downtrend = last.close < trend_mean

        # Same-timeframe close breakout gate.
        prior_high_close = max(b.close for b in prior_trend)
        prior_low_close = min(b.close for b in prior_trend)
        close_breakout_long = last.close > prior_high_close
        close_breakout_short = last.close < prior_low_close

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        # Price direction, order flow, fast trend, and a new local closing
        # extreme must all agree.
        if (
            body >= threshold
            and last.delta >= 0
            and fast_uptrend
            and close_breakout_long
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
            and close_breakout_short
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
