"""Momentum with ATR body, delta, fast price trend, persistent flow,
and a trailing protective stop on the engine bracket.

Builds on run #12, the best prior trial, preserving its exact entry filters and
initial bracket distances. The change is in the exit: once the entry fills, the
protective stop leg of the OCO bracket is trailed upward for longs (downward for
shorts) after a small activation profit, while the fixed profit target remains
at 3.0 ATR. This is intended to keep the proven entry edge while reducing the
tail loss on trades that move partway toward the target and then reverse.

Trade-off: a trailing stop may exit before the fixed target on a pullback that
would have recovered. The trail is therefore conservative: it only starts after
price has moved 1.0 ATR in the trade's favor, then trails at 2.0 ATR from the
peak/trough. The bracket legs are GTC so the position does not lose protection
at a session close.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import OrderType


class Strategy:
    """Momentum with ATR, delta, persistent flow, and a trailing stop."""

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
        trail_mult: float = 2.0,
        activation_mult: float = 1.0,
        min_stop_distance: float = 0.25,
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
        self.trail_mult = trail_mult
        self.activation_mult = activation_mult
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.session_margin_frac = session_margin_frac

        # Position/order state for the trailing stop. All optional values are
        # explicitly annotated so the static checker can prove no None leak.
        self.entry_id: Optional[int] = None
        self.entry_atr: Optional[float] = None
        self.entry_price: Optional[float] = None
        self.direction: int = 0
        self.peak: Optional[float] = None
        self.trough: Optional[float] = None
        self.initial_stop: Optional[float] = None
        self.trail_stop_price: Optional[float] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Manage an open position: trail the protective stop if we can.
        if ctx.position:
            self._manage_trail(ctx)
            return

        # No position, but there is still a working order (entry is resting).
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

        # Price direction, trigger-bar order flow, fast price trend, cumulative
        # flow, and recent flow persistence must all agree.
        if (
            body >= threshold
            and last.delta > 0
            and fast_uptrend
            and flow_delta > 0
            and positive_flow_bars >= flow_min_bars
        ):
            oid = ctx.buy(
                size=self.size,
                tif="GTC",
                stop_offset=stop,
                target_offset=target,
                tag="momentum-long",
            )
            if oid is not None:
                self.entry_id = oid
                self.entry_atr = atr
        elif (
            body <= -threshold
            and last.delta < 0
            and fast_downtrend
            and flow_delta < 0
            and negative_flow_bars >= flow_min_bars
        ):
            oid = ctx.sell(
                size=self.size,
                tif="GTC",
                stop_offset=stop,
                target_offset=target,
                tag="momentum-short",
            )
            if oid is not None:
                self.entry_id = oid
                self.entry_atr = atr

    def _manage_trail(self, ctx) -> None:
        """Trail the protective stop leg of the open bracket."""
        if self.entry_id is None:
            return
        entry_id = self.entry_id
        entry_atr = self.entry_atr
        if entry_atr is None:
            return
        entry_price = self.entry_price
        if entry_price is None:
            return
        trail_stop_price = self.trail_stop_price
        if trail_stop_price is None:
            return
        initial_stop = self.initial_stop
        if initial_stop is None:
            return

        hist = ctx.history(self.interval)
        if not len(hist):
            return
        bar = hist[-1]
        last_price = ctx.last_price

        if self.direction > 0:
            # Long: trail the sell-stop upward, never down.
            peak = self.peak
            if peak is None:
                peak = bar.high
                self.peak = peak
            else:
                peak = max(peak, bar.high)
                self.peak = peak

            candidate = peak - self.trail_mult * entry_atr
            if peak - entry_price < self.activation_mult * entry_atr:
                new_trigger = initial_stop
            else:
                new_trigger = max(initial_stop, candidate)
            # A sell-stop must stay below the current price.
            new_trigger = min(new_trigger, last_price - self.min_stop_distance)
            if new_trigger > trail_stop_price + 1e-9:
                stops = self._stop_legs(ctx, entry_id)
                if stops:
                    ctx.modify(stops[0].id, trigger=new_trigger)
                    self.trail_stop_price = new_trigger
        else:
            # Short: trail the buy-stop downward, never up.
            trough = self.trough
            if trough is None:
                trough = bar.low
                self.trough = trough
            else:
                trough = min(trough, bar.low)
                self.trough = trough

            candidate = trough + self.trail_mult * entry_atr
            if entry_price - trough < self.activation_mult * entry_atr:
                new_trigger = initial_stop
            else:
                new_trigger = min(initial_stop, candidate)
            # A buy-stop must stay above the current price.
            new_trigger = max(new_trigger, last_price + self.min_stop_distance)
            if new_trigger < trail_stop_price - 1e-9:
                stops = self._stop_legs(ctx, entry_id)
                if stops:
                    ctx.modify(stops[0].id, trigger=new_trigger)
                    self.trail_stop_price = new_trigger

    def _stop_legs(self, ctx, entry_id: int):
        """Return the protective stop leg of the bracket, if it is still working."""
        return tuple(
            order
            for order in ctx.find_orders(parent_id=entry_id)
            if order.order_type == OrderType.STOP
        )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        if fill.position != 0:
            # This is the entry fill. Record the fill price and initialize the
            # trailing-stop state from the fill, exactly as the engine's bracket
            # stop was armed.
            self.entry_id = fill.order_id
            self.entry_price = float(fill.price)
            self.direction = 1 if fill.position > 0 else -1
            self.peak = self.entry_price
            self.trough = self.entry_price

            if self.entry_atr is None:
                # Fallback; normally set at order placement.
                return

            if self.direction > 0:
                self.initial_stop = self.entry_price - self.stop_mult * self.entry_atr
                self.trail_stop_price = self.initial_stop
            else:
                self.initial_stop = self.entry_price + self.stop_mult * self.entry_atr
                self.trail_stop_price = self.initial_stop
        else:
            # Position is flat after this fill; reset trailing state.
            self.entry_id = None
            self.entry_atr = None
            self.entry_price = None
            self.direction = 0
            self.peak = None
            self.trough = None
            self.initial_stop = None
            self.trail_stop_price = None

    def on_lifecycle(self, ctx) -> None:
        pass
