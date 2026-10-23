"""A refined momentum strategy building on the best prior run (#6).

The prior best used 1m bars, an 8-bar ATR estimate, a body threshold of
0.20 * ATR with signed order-flow confirmation, ATR-scaled brackets
(stop 1.5 ATR / target 3.0 ATR), and a late-session gate. This version
keeps that core but adds an explicit trend filter:

    long  only if the trigger bar closes above the average close of the
          preceding 12 bars;
    short only if it closes below that average.

The intent is to remove counter-trend momentum bars that satisfy the body
and delta filter but fire against the prevailing short-term direction.
The rest of the execution remains identical to #6: the engine arms the
bracket at the fill, and no fresh risk is taken in the final 10% of a
session.
"""

from __future__ import annotations


class Strategy:
    """Momentum with ATR-scaled entries, delta confirmation, and a trend gate."""

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

        # Trend alignment, measured on closes before the trigger bar.
        prior_trend = hist.last(self.trend_window + 1)[:-1]
        if len(prior_trend) < self.trend_window:
            return
        trend_mean = sum(b.close for b in prior_trend) / self.trend_window
        uptrend = last.close > trend_mean
        downtrend = last.close < trend_mean

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        # Price direction and signed order flow must agree, and the move must
        # be with the short-term trend.
        if body >= threshold and last.delta >= 0 and uptrend:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-long",
            )
        elif body <= -threshold and last.delta <= 0 and downtrend:
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
