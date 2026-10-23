"""A momentum/order-flow strategy built on the best prior configuration (#34),
with additional flow-quality filters that remove entries whose signed volume is
nearly zero relative to total volume, or whose prior-bar flow imbalance is weak
relative to total delta. The core ATR/delta logic, session-end flattening and
1.5R/3.0R bracket are unchanged.

The new filters are deliberately mild: they only reject signals where order
flow points in the right direction but is so small it is effectively noise.
They should be cheap, and on data where all signals are strong the strategy
reduces exactly to #34.
"""

from __future__ import annotations


class Strategy:
    """ATR / delta momentum with weak order-flow rejection."""

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
        session_margin_frac: float = 0.10,
        close_margin_frac: float = 0.05,
        min_delta_frac: float = 0.10,
        flow_strength_min: float = 0.10,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.trend_window = trend_window
        self.flow_window = flow_window
        self.flow_min_frac = flow_min_frac
        self.entry_frac = entry_frac
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.session_margin_frac = session_margin_frac
        self.close_margin_frac = close_margin_frac
        self.min_delta_frac = min_delta_frac
        self.flow_strength_min = flow_strength_min
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _atr(self, ctx) -> float | None:
        """Return ATR over the bars immediately preceding the last closed bar."""
        hist = ctx.history(self.interval)
        recent = hist.last(self.lookback + 1)
        prior = recent[:-1]
        if len(prior) < 2:
            return None

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
            return None
        atr = sum(trs) / len(trs)
        return atr if atr > 0 else None

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Manage an open position: flatten before the session closes rather
        # than holding through the boundary.
        if ctx.position:
            session_len = ctx.session_end - ctx.session_start
            if session_len > 0:
                remaining = ctx.session_end - ctx.ts
                if remaining < session_len * self.close_margin_frac:
                    ctx.close()
            return

        # One position at a time; the bracket handles the exits otherwise.
        if ctx.working_order_ids:
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

        atr = self._atr(ctx)
        if atr is None:
            return

        recent = hist.last(self.lookback + 1)
        prior = recent[:-1]
        if len(prior) < 2:
            return

        last = recent[-1]
        body = last.close - last.open
        threshold = self.entry_frac * atr

        # Weak-flow rejection on the trigger bar: the bar's signed volume must
        # be a meaningful fraction of its total volume, not just a one-tick
        # positive delta.
        if last.volume <= 0:
            return
        delta_frac = last.delta / last.volume

        # Fast price trend: close above/below the average of previous closes.
        prior_trend = hist.last(self.trend_window + 1)[:-1]
        if len(prior_trend) < self.trend_window:
            return
        trend_mean = sum(b.close for b in prior_trend) / self.trend_window
        fast_uptrend = last.close > trend_mean
        fast_downtrend = last.close < trend_mean

        # Short-term order-flow persistence from previous bars, excluding the
        # trigger bar itself.
        prior_flow = hist.last(self.flow_window + 1)[:-1]
        if len(prior_flow) < self.flow_window:
            return

        flow_delta = sum(b.delta for b in prior_flow)
        flow_abs_delta = sum(abs(b.delta) for b in prior_flow)
        positive_flow_bars = sum(1 for b in prior_flow if b.delta > 0)
        negative_flow_bars = sum(1 for b in prior_flow if b.delta < 0)
        flow_min_bars = max(1, int(self.flow_window * self.flow_min_frac))

        # Net flow relative to gross flow; avoids acting on a near-zero net
        # delta that just happens to have the same sign as the signal.
        if flow_abs_delta > 0:
            flow_strength = flow_delta / flow_abs_delta
        else:
            flow_strength = 0.0

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        if (
            body >= threshold
            and last.delta > 0
            and delta_frac >= self.min_delta_frac
            and fast_uptrend
            and flow_delta > 0
            and positive_flow_bars >= flow_min_bars
            and flow_strength >= self.flow_strength_min
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
            and delta_frac <= -self.min_delta_frac
            and fast_downtrend
            and flow_delta < 0
            and negative_flow_bars >= flow_min_bars
            and flow_strength <= -self.flow_strength_min
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
