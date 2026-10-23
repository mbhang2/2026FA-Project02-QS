"""Builds on the best prior trial (#34): ATR-scaled momentum with trigger-bar
delta, a fast 1m price trend, a 1m order-flow persistence window, a 1.5R /
3.0R bracket, and session-end flattening in the final 5%.

This iteration adds one carefully scoped trailing stop. The fixed bracket is
left unchanged until the trade moves far enough in our favour. Once the market
has moved `trail_trigger_atr` multiples of ATR past entry, the protective stop
is ratcheted to follow the price at `trail_distance_atr` multiples of ATR.  The
chosen defaults are deliberately late and wide: they activate only after a
strong move and then trail behind the bar close, so they should lock in part of
a large winner without cutting normal pullbacks that would otherwise reach the
fixed 3R target.

The entry model is otherwise identical to #34. No cooldown, no max-hold, and
no trigger-bar flow-purity floor are added; those were tested in #35 and
materially reduced performance.
"""

from __future__ import annotations

from src.core.types import OrderType, Side


class Strategy:
    """ATR / delta momentum with a late protective-stop ratchet."""

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
        trail_trigger_atr: float = 1.5,
        trail_distance_atr: float = 2.0,
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
        self.trail_trigger_atr = trail_trigger_atr
        self.trail_distance_atr = trail_distance_atr
        self.size = size

        # Per-position state for the trailing stop.
        self._entry_id: int | None = None
        self._entry_side: Side | None = None
        self._entry_price: float | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _atr(self, ctx) -> float | None:
        """Return the current ATR estimate, or None when history is too short."""
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

    def _manage_open_position(self, ctx) -> None:
        """Flatten near the session close and ratchet the protective stop."""
        session_len = ctx.session_end - ctx.session_start
        if session_len > 0:
            remaining = ctx.session_end - ctx.ts
            if remaining < session_len * self.close_margin_frac:
                ctx.close()
                return

        if self._entry_side is None or self._entry_price is None:
            return

        # The bracket legs are parented to the already-filled entry order.
        children = ctx.find_orders(parent_id=self._entry_id)
        stop_legs = [o for o in children if o.order_type in (OrderType.STOP, "stop")]
        if not stop_legs:
            return

        stop_order = stop_legs[0]
        current_stop = getattr(stop_order, "stop_price", None)
        if current_stop is None:
            return

        atr = self._atr(ctx)
        if atr is None:
            return

        if self._entry_side is Side.BUY:
            favorable = ctx.last_price - self._entry_price
            if favorable < self.trail_trigger_atr * atr:
                return
            desired = ctx.last_price - self.trail_distance_atr * atr
            new_stop = max(current_stop, desired)
            if new_stop > current_stop + 1e-12:
                ctx.modify(stop_order.id, trigger=new_stop)
        else:
            favorable = self._entry_price - ctx.last_price
            if favorable < self.trail_trigger_atr * atr:
                return
            desired = ctx.last_price + self.trail_distance_atr * atr
            new_stop = min(current_stop, desired)
            if new_stop < current_stop - 1e-12:
                ctx.modify(stop_order.id, trigger=new_stop)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Manage an existing position first.
        if ctx.position:
            self._manage_open_position(ctx)
            return

        # One position at a time; the engine bracket handles fixed exits.
        if ctx.working_order_ids:
            return

        # Avoid fresh risk in the last part of a session.
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

        # Fast price trend relative to the preceding closes.
        prior_trend = hist.last(self.trend_window + 1)[:-1]
        if len(prior_trend) < self.trend_window:
            return
        trend_mean = sum(b.close for b in prior_trend) / self.trend_window
        fast_uptrend = last.close > trend_mean
        fast_downtrend = last.close < trend_mean

        # Short-term order-flow persistence, excluding the trigger bar.
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
        ):
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="momentum-short",
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        if fill.position != 0:
            # Opening fill: start tracking the entry for the trailing stop.
            self._entry_id = fill.order_id
            self._entry_side = fill.side
            self._entry_price = fill.price
        else:
            # Position was closed.
            self._entry_id = None
            self._entry_side = None
            self._entry_price = None

    def on_lifecycle(self, ctx) -> None:
        pass
