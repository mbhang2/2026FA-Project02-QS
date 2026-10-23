"""Momentum with ATR body, delta, fast trend, persistent flow, and a managed
trailing stop.

Builds on the best prior trial (#12), keeping its exact entry filters: ATR
body threshold, trigger-bar signed delta, fast price trend, prior flow-window
persistence, bracket distances, and late-session gate.

The only change is exit management. The engine-armed bracket still protects
the position from the instant it fills, but now we manage the protective stop
leg once the position is open:

- if price reaches +1.5R (or -1.5R for shorts), the stop moves to breakeven
- if price reaches +2.2R, the stop trails behind the market by 1.5R

The target remains 3.0R. The intention is to cut losing trades earlier while
leaving the target intact, which is the standard "let winners run, cap losers"
asymmetry.
"""

from __future__ import annotations

from src.core.types import OrderType


class Strategy:
    """Momentum with ATR/delta confirmation and a trailing protective stop."""

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
        breakeven_profit_r: float = 1.5,
        trail_trigger_r: float = 2.2,
        trail_stop_mult: float = 1.5,
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
        self.breakeven_profit_r = breakeven_profit_r
        self.trail_trigger_r = trail_trigger_r
        self.trail_stop_mult = trail_stop_mult
        self.size = size
        self.session_margin_frac = session_margin_frac

        self.entry_id: int | None = None
        self.entry_atr: float | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.position:
            self._manage_position(ctx)
            return

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

    def _manage_position(self, ctx) -> None:
        """Trail the protective stop once the engine-armed bracket is working."""
        if self.entry_id is None or self.entry_atr is None:
            return

        atr = self.entry_atr
        if atr <= 0:
            return

        legs = ctx.find_orders(parent_id=self.entry_id)
        if not legs:
            return

        stop_leg = None
        for leg in legs:
            if leg.order_type is OrderType.STOP:
                stop_leg = leg
                break

        if stop_leg is None or stop_leg.stop_price is None:
            return

        current_stop = float(stop_leg.stop_price)
        entry = ctx.average_entry_price
        last_px = ctx.last_price

        desired = current_stop

        if ctx.position > 0:
            profit_r = (last_px - entry) / atr

            if profit_r >= self.breakeven_profit_r:
                desired = max(desired, entry)

            if profit_r >= self.trail_trigger_r:
                desired = max(desired, last_px - self.trail_stop_mult * atr)

            # A protective sell stop must remain below the current price.
            desired = min(desired, last_px - 0.01)

            if desired > current_stop + 1e-6:
                ctx.modify(stop_leg.id, trigger=desired)

        elif ctx.position < 0:
            profit_r = (entry - last_px) / atr

            if profit_r >= self.breakeven_profit_r:
                desired = min(desired, entry)

            if profit_r >= self.trail_trigger_r:
                desired = min(desired, last_px + self.trail_stop_mult * atr)

            # A protective buy stop must remain above the current price.
            desired = max(desired, last_px + 0.01)

            if desired < current_stop - 1e-6:
                ctx.modify(stop_leg.id, trigger=desired)

    def on_fill(self, ctx) -> None:
        if ctx.fill.position == 0:
            self.entry_id = None
            self.entry_atr = None

    def on_lifecycle(self, ctx) -> None:
        pass
