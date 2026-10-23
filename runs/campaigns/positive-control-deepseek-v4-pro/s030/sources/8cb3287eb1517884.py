from __future__ import annotations


class Strategy:
    """Momentum with ATR body, order-flow, fast trend, and a close-location gate.

    This builds on run #7, which was the best prior trial. #7 enters on a
    strong directional 1m bar (body >= 0.20 ATR, delta sign confirming) that
    also closes beyond the 12-bar average close. That structure is kept intact
    here.

    The new gate is deliberately small and fast: for a long, the trigger bar
    must close in the top 55% of its own high-low range; for a short, in the
    bottom 45%. This removes bars with large bodies but long wicks against the
    close -- bars that satisfy the body/delta/trend conditions but showed
    rejection inside the interval. The bracket is still armed by the engine at
    the fill, so the position is protected from that instant.
    """

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
        close_loc_frac: float = 0.55,
    ):
        self.interval = interval
        self.lookback = lookback
        self.trend_window = trend_window
        self.entry_frac = entry_frac
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.session_margin_frac = session_margin_frac
        self.close_loc_frac = close_loc_frac

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

        # Close location within the trigger bar's own range. A zero-range bar
        # cannot satisfy the body threshold anyway, but guard explicitly.
        rng = last.high - last.low
        if rng <= 0:
            return
        close_loc = (last.close - last.low) / rng

        # Fast trend alignment: close above/below the average of the previous
        # trend_window closes.
        prior_trend = hist.last(self.trend_window + 1)[:-1]
        if len(prior_trend) < self.trend_window:
            return
        trend_mean = sum(b.close for b in prior_trend) / self.trend_window
        fast_uptrend = last.close > trend_mean
        fast_downtrend = last.close < trend_mean

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        # Everything must agree: price direction, order flow, fast trend, and
        # the bar must close near the directional extreme of its own range.
        if (
            body >= threshold
            and last.delta >= 0
            and fast_uptrend
            and close_loc >= self.close_loc_frac
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
            and close_loc <= (1.0 - self.close_loc_frac)
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
