"""Builds on the best prior trial (#34): ATR-scaled momentum with trigger-bar
delta, a fast 1m price trend, a 1m order-flow persistence window, a 1.5R / 3.0R
bracket, and session-end flattening in the final 5%.

The new element is a defensive breakeven trail. Once a position has moved one
ATR in its favour, the protective stop is raised to the entry price. This removes
the remaining 1.5R downside from the trades that showed initial promise but
round-tripped, at the cost of scratching some trades that would have recovered
and reached the target after a pullback.

The entry model is otherwise identical to #34.
"""

from __future__ import annotations

from src.core.types import OrderType, Side


class Strategy:
    """ATR / delta momentum with breakeven trailing."""

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
        breakeven_atr: float = 1.0,
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
        self.breakeven_atr = breakeven_atr
        self.size = size

        # Position management state. Floats use sentinel 0.0 rather than None
        # so arithmetic stays type-safe.
        self._has_position: bool = False
        self._position_entry_id: int | None = None
        self._entry_side: Side | None = None
        self._entry_price: float = 0.0
        self._entry_atr: float = 0.0
        self._entry_stop_dist: float = 0.0
        self._stop_level: float = 0.0

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

    def _manage_open_position(self, ctx) -> None:
        """Trail the protective stop to breakeven after an initial ATR win."""
        if not self._has_position:
            return

        entry_id = self._position_entry_id
        side = self._entry_side
        if entry_id is None or side is None:
            return

        stops = ctx.find_orders(
            parent_id=entry_id,
            order_type=OrderType.STOP,
        )
        if not stops:
            return

        entry_price = self._entry_price
        atr = self._entry_atr
        stop_level = self._stop_level

        if side is Side.BUY:
            # Long: raise the sell-stop from entry-1.5R to entry once price
            # has moved one ATR above entry.
            if ctx.last_price >= entry_price + self.breakeven_atr * atr:
                new_stop = max(stop_level, entry_price)
                if new_stop != stop_level:
                    ctx.modify(stops[0].id, trigger=new_stop)
                    self._stop_level = new_stop
        else:
            # Short: lower the buy-stop from entry+1.5R to entry once price
            # has moved one ATR below entry.
            if ctx.last_price <= entry_price - self.breakeven_atr * atr:
                new_stop = min(stop_level, entry_price)
                if new_stop != stop_level:
                    ctx.modify(stops[0].id, trigger=new_stop)
                    self._stop_level = new_stop

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Manage an open position.
        if ctx.position:
            session_len = ctx.session_end - ctx.session_start
            if session_len > 0:
                remaining = ctx.session_end - ctx.ts
                if remaining < session_len * self.close_margin_frac:
                    ctx.close()
                    return
            self._manage_open_position(ctx)
            return

        # Do not take new entries while any order is working.
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
        positive_flow_bars = sum(1 for b in prior_flow if b.delta > 0)
        negative_flow_bars = sum(1 for b in prior_flow if b.delta < 0)
        flow_min_bars = max(1, int(self.flow_window * self.flow_min_frac))

        stop = self.stop_mult * atr
        target = self.target_mult * atr

        # Remember the position-sizing parameters for on_fill.
        self._entry_atr = atr
        self._entry_stop_dist = stop

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
            if not self._has_position:
                self._has_position = True
                self._position_entry_id = fill.order_id
                self._entry_side = fill.side
                self._entry_price = fill.price

                if fill.side is Side.BUY:
                    self._stop_level = self._entry_price - self._entry_stop_dist
                else:
                    self._stop_level = self._entry_price + self._entry_stop_dist
        else:
            # Any exit fill flattens the position and makes the state stale.
            self._has_position = False
            self._position_entry_id = None
            self._entry_side = None
            self._entry_price = 0.0
            self._entry_atr = 0.0
            self._entry_stop_dist = 0.0
            self._stop_level = 0.0

    def on_lifecycle(self, ctx) -> None:
        pass
