"""Continue the isolated profit-target sweep on the ATR/delta momentum engine.

#87 is the best result so far with target_mult=3.4995R while keeping every other
decision unchanged: 1m signal bars, 30s management bars, stop 1.30R, trailing
activation at 1.0R, trail width 1.5R, entry/trend/flow filters identical.

The measured local slope from #86 (3.4990R, 4.2988) to #87 (3.4995R, 4.3019)
is about 6.2 score points per full R of target. #68/#77 showed a cliff at
3.50R (2.0897). The next conservative probe is 3.49975R: halfway between the
current best and the observed cliff. All other parameters are exactly those
of #87.
"""

from __future__ import annotations


class Strategy:
    """ATR / delta momentum with a 3.49975R profit target."""

    def __init__(
        self,
        *,
        interval: str = "1m",
        manage_interval: str = "30s",
        lookback: int = 8,
        trend_window: int = 12,
        flow_window: int = 6,
        flow_min_frac: float = 0.5,
        entry_frac: float = 0.20,
        stop_mult: float = 1.30,
        target_mult: float = 3.49975,
        size: int = 1,
        session_margin_frac: float = 0.10,
        close_margin_frac: float = 0.05,
        trail_activation_r: float = 1.0,
        trail_mult: float = 1.5,
    ):
        self.interval = interval
        self.manage_interval = manage_interval
        self.lookback = lookback
        self.trend_window = trend_window
        self.flow_window = flow_window
        self.flow_min_frac = flow_min_frac
        self.entry_frac = entry_frac
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.session_margin_frac = session_margin_frac
        self.close_margin_frac = close_margin_frac
        self.trail_activation_r = trail_activation_r
        self.trail_mult = trail_mult

        self.entry_id = None
        self.entry_atr = None
        self.best_price = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.manage_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.position:
            self._manage_open_position(ctx)
            return

        if ctx.working_order_ids:
            return

        if self.interval not in ctx.bars:
            return

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

        prior_trend = hist.last(self.trend_window + 1)[:-1]
        if len(prior_trend) < self.trend_window:
            return
        trend_mean = sum(b.close for b in prior_trend) / self.trend_window
        fast_uptrend = last.close > trend_mean
        fast_downtrend = last.close < trend_mean

        prior_flow = hist.last(self.flow_window + 1)[:-1]
        if len(prior_flow) < self.flow_window:
            return

        flow_delta = sum(b.delta for b in prior_flow)
        positive_flow_bars = sum(1 for b in prior_flow if b.delta > 0)
        negative_flow_bars = sum(1 for b in prior_flow if b.delta < 0)
        flow_min_bars = max(1, int(self.flow_window * self.flow_min_frac))

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        if (
            body >= threshold
            and last.delta > 0
            and fast_uptrend
            and flow_delta > 0
            and positive_flow_bars >= flow_min_bars
        ):
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-long",
            )
            if order_id is not None:
                self.entry_id = order_id
                self.entry_atr = atr
                self.best_price = None

        elif (
            body <= -threshold
            and last.delta < 0
            and fast_downtrend
            and flow_delta < 0
            and negative_flow_bars >= flow_min_bars
        ):
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-short",
            )
            if order_id is not None:
                self.entry_id = order_id
                self.entry_atr = atr
                self.best_price = None

    def _manage_open_position(self, ctx) -> None:
        session_len = ctx.session_end - ctx.session_start
        if session_len > 0:
            remaining = ctx.session_end - ctx.ts
            if remaining < session_len * self.close_margin_frac:
                ctx.close()
                return

        if self.entry_id is None or self.entry_atr is None:
            return

        management_history = ctx.history(self.manage_interval)
        if len(management_history) < 1:
            return
        bar = management_history[-1]

        position = ctx.position
        if position > 0:
            if self.best_price is None:
                self.best_price = ctx.average_entry_price
            self.best_price = max(self.best_price, bar.high)

            if (
                self.best_price - ctx.average_entry_price
                < self.trail_activation_r * self.entry_atr
            ):
                return

            new_stop = self.best_price - self.trail_mult * self.entry_atr
            original_stop = ctx.average_entry_price - self.stop_mult * self.entry_atr
            new_stop = max(new_stop, original_stop)

        elif position < 0:
            if self.best_price is None:
                self.best_price = ctx.average_entry_price
            self.best_price = min(self.best_price, bar.low)

            if (
                ctx.average_entry_price - self.best_price
                < self.trail_activation_r * self.entry_atr
            ):
                return

            new_stop = self.best_price + self.trail_mult * self.entry_atr
            original_stop = ctx.average_entry_price + self.stop_mult * self.entry_atr
            new_stop = min(new_stop, original_stop)

        else:
            return

        legs = ctx.find_orders(parent_id=self.entry_id)
        for leg in legs:
            current_stop = leg.stop_price
            if current_stop is None:
                continue

            if position > 0 and new_stop > current_stop:
                ctx.modify(leg.id, trigger=new_stop)
            elif position < 0 and new_stop < current_stop:
                ctx.modify(leg.id, trigger=new_stop)
            return

    def on_fill(self, ctx) -> None:
        if ctx.fill.position == 0:
            self.entry_id = None
            self.entry_atr = None
            self.best_price = None

    def on_lifecycle(self, ctx) -> None:
        pass
