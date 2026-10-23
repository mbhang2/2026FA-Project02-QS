"""Momentum with ATR body, delta, fast trend, persistent flow, breakout, and fresh flow.

Builds on the best prior trial (#12), keeping its exact ATR scaling, fast price
trend, flow-window persistence, trigger-bar signed delta, and late-session gate.
This version adds two conditions meant to remove signals where the recent flow
is no longer aligned:

1. Breakout confirmation: the trigger bar must close beyond the highest high
   (for longs) or lowest low (for shorts) of the preceding lookback bars, not
   merely above/below the average close.
2. Fresh flow confirmation: the last bar before the trigger must have signed
   delta on the signal side, so a signal cannot rest only on older flow bars
   while the most recent bar is already turning against it.

Everything else -- 1m bars, 8-bar ATR, 0.20 ATR body threshold, 12-bar fast
price trend, 6-bar flow window with half-sign agreement, 1.5/3.0 ATR bracket
offsets, and the late-session gate -- is unchanged from the best prior run.
"""

from __future__ import annotations


class Strategy:
    """Momentum with order-flow persistence, breakout, and fresh-flow filters."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        lookback: int = 8,
        trend_window: int = 12,
        flow_window: int = 6,
        flow_min_frac: float = 0.5,
        entry_frac: float = 0.20,
        stop_mult: float = 1.5,
        target_mult: float = 3.0,
        size: int = 1,
        session_margin_frac: float = 0.10,
    ):
        self.interval = interval
        self.lookback = lookback
        self.trend_window = trend_window
        self.flow_window = flow_window
        self.flow_min_frac = flow_min_frac
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

        # Do not take fresh risk in the last part of a session.
        session_len = ctx.session_end - ctx.session_start
        if session_len > 0:
            remaining = ctx.session_end - ctx.ts
            if remaining < session_len * self.session_margin_frac:
                return

        hist = ctx.history(self.interval)
        needed = max(
            self.lookback + 1,
            self.trend_window + 1,
            self.flow_window + 1,
        )
        if len(hist) < needed:
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

        # Fast price trend: close above/below the average of previous closes.
        prior_trend = hist.last(self.trend_window + 1)[:-1]
        if len(prior_trend) < self.trend_window:
            return
        trend_mean = sum(b.close for b in prior_trend) / self.trend_window
        fast_uptrend = last.close > trend_mean
        fast_downtrend = last.close < trend_mean

        # Short-term order-flow persistence from previous bars, excluding trigger.
        prior_flow = hist.last(self.flow_window + 1)[:-1]
        if len(prior_flow) < self.flow_window:
            return

        flow_delta = sum(b.delta for b in prior_flow)
        positive_flow_bars = sum(1 for b in prior_flow if b.delta > 0)
        negative_flow_bars = sum(1 for b in prior_flow if b.delta < 0)
        flow_min_bars = max(1, int(self.flow_window * self.flow_min_frac))

        # Fresh flow: the bar immediately before the trigger must agree.
        fresh_flow_up = prior_flow[-1].delta > 0
        fresh_flow_down = prior_flow[-1].delta < 0

        # Breakout: the trigger close must clear the prior lookback extremes.
        recent_high = max(b.high for b in prior)
        recent_low = min(b.low for b in prior)

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        # Price direction, trigger-bar order flow, fast price trend, cumulative
        # flow, recent flow persistence, fresh flow, and breakout must all agree.
        if (
            body >= threshold
            and last.delta > 0
            and fast_uptrend
            and flow_delta > 0
            and positive_flow_bars >= flow_min_bars
            and fresh_flow_up
            and last.close > recent_high
        ):
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-long",
            )
        elif (
            body <= -threshold
            and last.delta < 0
            and fast_downtrend
            and flow_delta < 0
            and negative_flow_bars >= flow_min_bars
            and fresh_flow_down
            and last.close < recent_low
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
