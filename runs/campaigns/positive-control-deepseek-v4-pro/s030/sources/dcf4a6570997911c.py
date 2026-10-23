"""Momentum with ATR body, fast trend, persistent order flow, and net flow quality.

Builds on the best prior run (#12), which required a strong trigger-bar body,
strict trigger-side delta, a fast price trend, and at least half of the prior
flow-window bars agreeing with the signal.

This version adds one more order-flow quality guard: the cumulative signed delta
over the prior flow window must be a meaningful fraction of total absolute
delta. That keeps the trade when the window is persistently one-sided, while
filtering windows where a single high-volume bar dominates an otherwise mixed
flow. Without this, a six-bar window with three slightly positive bars and one
large negative bar could still pass the old `flow_delta > 0` test even though
the recent order flow is not genuinely persistent.

Everything else -- 1m bars, 8-bar ATR, 0.20 ATR body threshold, 12-bar price
trend, 6-bar flow window, 1.5/3.0 ATR bracket offsets, and late-session gate --
is unchanged from the best prior trial.
"""

from __future__ import annotations


class Strategy:
    """Momentum with ATR, delta, trend, persistent flow, and net flow quality."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        lookback: int = 8,
        trend_window: int = 12,
        flow_window: int = 6,
        flow_min_frac: float = 0.5,
        flow_net_frac: float = 0.15,
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
        self.flow_net_frac = flow_net_frac
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

        flow_delta = sum(float(b.delta) for b in prior_flow)
        flow_abs = sum(abs(float(b.delta)) for b in prior_flow)
        positive_flow_bars = sum(1 for b in prior_flow if b.delta > 0)
        negative_flow_bars = sum(1 for b in prior_flow if b.delta < 0)
        flow_min_bars = max(1, int(self.flow_window * self.flow_min_frac))

        if flow_abs > 0:
            net_flow = flow_delta / flow_abs
        else:
            net_flow = 0.0

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        if (
            body >= threshold
            and last.delta > 0
            and fast_uptrend
            and flow_delta > 0
            and net_flow >= self.flow_net_frac
            and positive_flow_bars >= flow_min_bars
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
            and net_flow <= -self.flow_net_frac
            and negative_flow_bars >= flow_min_bars
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
