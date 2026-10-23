"""Proposal #76: #75 with protective stops cancelled before session-end flatten.

Keeps #61's verified 5m breakout/trend entry, 0.90 ATR initial protective stop,
1m chandelier trail, and slow-close breakeven exactly.  Keeps #75's idea of
flattening losing positions near the session close, but makes the final exit
safer: the reduce-only protective stop is cancelled *before* queueing the
market close, so there is never both a resting stop and a market close working
against the same position at the same instant.

The only parameter change from #75 is `session_close_margin_s` from 75 to 45:
act only on the final 1m bar close of a session, rather than also acting on the
penultimate one.  That trims the session-end intervention back closer to the
best-scoring #61 behaviour, while still avoiding carrying a loser across the
session boundary.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        slow: str = "5m",
        fast: str = "1m",
        lookback: int = 20,
        breakout_bars: int = 3,
        stop_mult: float = 0.90,
        breakeven_atr_mult: float = 0.30,
        trail_atr_mult: float = 0.75,
        trail_atr_distance_mult: float = 0.50,
        size: int = 3,
        session_close_margin_s: float = 45.0,
    ):
        self.slow = slow
        self.fast = fast
        self.lookback = lookback
        self.breakout_bars = breakout_bars
        self.stop_mult = stop_mult
        self.breakeven_atr_mult = breakeven_atr_mult
        self.trail_atr_mult = trail_atr_mult
        self.trail_atr_distance_mult = trail_atr_distance_mult
        self.size = size
        self.session_close_margin_ns = int(session_close_margin_s * 1_000_000_000)

        self.entry_price: float | None = None
        self.entry_atr: float | None = None
        self.pending_atr: float | None = None
        self._last_position = 0
        self._last_session: int | None = None
        self._closing_at_session_end = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.slow)
        ctx.subscribe(self.fast)

    def _average_true_range(self, window) -> float | None:
        """Average true range over `window`.

        `window[0]` is used only as the previous close for `window[1]`, so the
        caller must pass one leading bar.
        """
        trs = []
        for i in range(1, len(window)):
            bar = window[i]
            prev_close = window[i - 1].close
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
            trs.append(tr)
        if not trs:
            return None
        return sum(trs) / len(trs)

    def _should_close_losing_at_session_end(self, ctx) -> bool:
        if ctx.ts < ctx.session_end - self.session_close_margin_ns:
            return False
        if self.entry_price is None:
            return False
        if ctx.position > 0:
            return ctx.last_price <= self.entry_price
        if ctx.position < 0:
            return ctx.last_price >= self.entry_price
        return False

    def _flatten_at_session_end(self, ctx) -> bool:
        """Cancel protection first, then queue a market close.

        The protective stop is reduce-only, so it cannot open a new position,
        but leaving it working while a market close is also working can create
        two orders competing for the same exit.  Cancelling first leaves a
        single unambiguous exit order.
        """
        for stop_order in ctx.find_orders(tag="protective-stop"):
            ctx.cancel(stop_order.id)
        close_id = ctx.close()
        return close_id is not None

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if self._last_session is not None and ctx.session != self._last_session:
            self._closing_at_session_end = False
        self._last_session = ctx.session

        self._last_position = ctx.position

        if not ctx.position:
            self._closing_at_session_end = False

        # If a session-end close is already working, do not interfere with it.
        if ctx.position and self._closing_at_session_end:
            return

        # Near the end of a session, cut losers rather than carry them through
        # the session boundary.
        if ctx.position and self._should_close_losing_at_session_end(ctx):
            if self._flatten_at_session_end(ctx):
                self._closing_at_session_end = True
            return

        slow_closed = self.slow in ctx.bars

        # Manage an open position on every bar close, fast or slow.
        if ctx.position:
            self._manage_open_position(ctx, slow_closed=slow_closed)
            return

        if ctx.working_order_ids:
            return

        # New entries are only taken on the slow bar close.
        if not slow_closed:
            return

        history = ctx.history(self.slow)
        if len(history) < self.lookback + 2:
            return

        window = history.last(self.lookback + 2)
        if len(window) < self.lookback + 2:
            return

        last = window[-1]

        # Full pre-breakout prefix includes one leading close so every one of
        # the `lookback` prior bars contributes a true range.
        prefix = window[:-1]
        prior = prefix[-self.lookback:]
        if len(prior) < self.lookback:
            return

        trend = sum(b.close for b in prior) / self.lookback

        atr_value = self._average_true_range(prefix)
        if atr_value is None or atr_value <= 0:
            return
        atr = atr_value

        stop_distance = atr * self.stop_mult
        if stop_distance <= 0:
            return

        recent = prior[-self.breakout_bars:]
        recent_high = max(b.high for b in recent)
        recent_low = min(b.low for b in recent)

        last_delta = last.delta
        recent_delta = sum(b.delta for b in recent)

        if (
            last.close > last.open
            and last_delta > 0
            and recent_delta > 0
            and last.close > trend
            and last.close > recent_high
        ):
            self.pending_atr = atr
            ctx.buy(size=self.size, tag="trend-long")
        elif (
            last.close < last.open
            and last_delta < 0
            and recent_delta < 0
            and last.close < trend
            and last.close < recent_low
        ):
            self.pending_atr = atr
            ctx.sell(size=self.size, tag="trend-short")

    def _manage_open_position(self, ctx, *, slow_closed: bool) -> None:
        entry_price = self.entry_price
        entry_atr = self.entry_atr
        if entry_price is None or entry_atr is None:
            return

        atr = entry_atr
        last = ctx.last_price

        if ctx.position > 0:
            stops = ctx.find_orders(tag="protective-stop")
            if not stops:
                return
            stop_order = stops[0]
            current = stop_order.stop_price
            if current is None:
                return

            new_trigger = current

            # The breakeven move waits for a slow-bar close.  The trail still
            # updates on every fast-bar close.
            if slow_closed and last >= entry_price + self.breakeven_atr_mult * atr:
                new_trigger = max(new_trigger, entry_price)
            if last >= entry_price + self.trail_atr_mult * atr:
                trail = last - self.trail_atr_distance_mult * atr
                new_trigger = max(new_trigger, trail)

            if new_trigger > current:
                ctx.modify(stop_order.id, trigger=new_trigger)

        elif ctx.position < 0:
            stops = ctx.find_orders(tag="protective-stop")
            if not stops:
                return
            stop_order = stops[0]
            current = stop_order.stop_price
            if current is None:
                return

            new_trigger = current

            if slow_closed and last <= entry_price - self.breakeven_atr_mult * atr:
                new_trigger = min(new_trigger, entry_price)
            if last <= entry_price - self.trail_atr_mult * atr:
                trail = last + self.trail_atr_distance_mult * atr
                new_trigger = min(new_trigger, trail)

            if new_trigger < current:
                ctx.modify(stop_order.id, trigger=new_trigger)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        previous_position = self._last_position
        self._last_position = fill.position

        if fill.position == 0:
            self._closing_at_session_end = False
            # If a manual close or stop fill leaves no position, remove any
            # stale reduce-only protective stop that may still be working.
            for stale in ctx.find_orders(tag="protective-stop"):
                ctx.cancel(stale.id)
            self.entry_price = None
            self.entry_atr = None
            self.pending_atr = None
            return

        if abs(fill.position) <= abs(previous_position):
            return

        if self.entry_price is None:
            self.entry_price = fill.price
            self.entry_atr = self.pending_atr

        if self.entry_atr is None:
            return

        distance = self.entry_atr * self.stop_mult
        if distance <= 0:
            return

        existing = ctx.find_orders(tag="protective-stop")
        if not existing:
            if fill.position > 0:
                ctx.sell(
                    size=abs(fill.position),
                    type="stop",
                    trigger=fill.price - distance,
                    tif="GTC",
                    reduce_only=True,
                    tag="protective-stop",
                )
            elif fill.position < 0:
                ctx.buy(
                    size=abs(fill.position),
                    type="stop",
                    trigger=fill.price + distance,
                    tif="GTC",
                    reduce_only=True,
                    tag="protective-stop",
                )
        else:
            stop_order = existing[0]
            if stop_order.size != abs(fill.position):
                ctx.modify(stop_order.id, size=abs(fill.position))

    def on_lifecycle(self, ctx) -> None:
        pass
