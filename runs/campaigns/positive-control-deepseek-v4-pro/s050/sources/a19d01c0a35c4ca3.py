"""Proposal: #38 plus a disciplined session-end exit.

The best-known strategy in this campaign, #38, runs an ATR-based chandelier
trail and breakeven move on 1-minute bars, but it never explicitly flattens
before the session close. A position that survives to the boundary is carried
across sessions with only its resting GTC stop for protection.

This version keeps #38's entry logic, ATR calculation, protective-stop
placement, breakeven move and 1-minute chandelier trail exactly. It adds two
changes:

1. The 1-minute management subscription is declared with `emit_empty=True`, so
   the strategy receives a callback in a quiet late-session minute even when
   nothing traded. That is what makes a time-based exit possible instead of
   "exiting late or not at all".

2. A session-end flat rule: when an open position remains with less than 120
   seconds until `ctx.session_end`, the strategy submits a market close order.
   The protective stop is left in place until the close fill arrives, so the
   position is not naked if the close order does not fill immediately. On the
   flat fill the protective stop is cancelled if still working.

Entry filters, ATR calculation, size, stop distance, breakeven threshold and
trailing parameters are otherwise identical to #38.
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
        breakeven_atr_mult: float = 0.35,
        trail_atr_mult: float = 0.75,
        trail_atr_distance_mult: float = 0.50,
        size: int = 3,
        session_exit_margin_ns: int = 120_000_000_000,
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
        self.session_exit_margin_ns = session_exit_margin_ns

        self.entry_price: float | None = None
        self.entry_atr: float | None = None
        self.pending_atr: float | None = None
        self._last_position = 0
        self._closing_for_session = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.slow)
        # emit_empty=True guarantees a callback in quiet late-session minutes,
        # which is the only way to flatten reliably before the close.
        ctx.subscribe(self.fast, emit_empty=True)

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

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        self._last_position = ctx.position

        if ctx.position:
            # If a session-end close order is already queued but not yet
            # filled, do not queue another one.
            if self._closing_for_session:
                return

            if ctx.session_end - ctx.ts < self.session_exit_margin_ns:
                self._closing_for_session = True
                ctx.close(tag="session-close")
                return

            self._manage_open_position(ctx)
            return

        if self._closing_for_session:
            self._closing_for_session = False

        if ctx.working_order_ids:
            return

        # New entries are only taken on the slow bar close.
        if self.slow not in ctx.bars:
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
        prefix = window[:-1]              # length lookback + 1
        prior = prefix[-self.lookback:]    # the bars just before the breakout
        if len(prior) < self.lookback:
            return

        trend = sum(b.close for b in prior) / self.lookback

        atr_value = self._average_true_range(prefix)
        if atr_value is None or atr_value <= 0:
            return
        atr = atr_value

        stop = atr * self.stop_mult
        if stop <= 0:
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

    def _manage_open_position(self, ctx) -> None:
        entry_price = self.entry_price
        entry_atr = self.entry_atr
        if entry_price is None or entry_atr is None:
            return

        atr = entry_atr
        last = ctx.last_price

        if ctx.position > 0:
            stops = ctx.find_orders(tag="protective-stop")
            if not stops:
                self._place_protective_stop(ctx, entry_price, atr)
                return
            stop_order = stops[0]
            current = stop_order.stop_price
            if current is None:
                return

            new_trigger = current
            if last >= entry_price + self.breakeven_atr_mult * atr:
                new_trigger = max(new_trigger, entry_price)
            if last >= entry_price + self.trail_atr_mult * atr:
                trail = last - self.trail_atr_distance_mult * atr
                new_trigger = max(new_trigger, trail)

            if new_trigger > current:
                ctx.modify(stop_order.id, trigger=new_trigger)

        elif ctx.position < 0:
            stops = ctx.find_orders(tag="protective-stop")
            if not stops:
                self._place_protective_stop(ctx, entry_price, atr)
                return
            stop_order = stops[0]
            current = stop_order.stop_price
            if current is None:
                return

            new_trigger = current
            if last <= entry_price - self.breakeven_atr_mult * atr:
                new_trigger = min(new_trigger, entry_price)
            if last <= entry_price - self.trail_atr_mult * atr:
                trail = last + self.trail_atr_distance_mult * atr
                new_trigger = min(new_trigger, trail)

            if new_trigger < current:
                ctx.modify(stop_order.id, trigger=new_trigger)

    def _place_protective_stop(self, ctx, entry_price: float, atr: float) -> None:
        """Arm a missing protective stop for an open position."""
        distance = atr * self.stop_mult
        if distance <= 0 or not ctx.position:
            return
        if ctx.position > 0:
            ctx.sell(
                size=abs(ctx.position),
                type="stop",
                trigger=entry_price - distance,
                tif="GTC",
                reduce_only=True,
                tag="protective-stop",
            )
        elif ctx.position < 0:
            ctx.buy(
                size=abs(ctx.position),
                type="stop",
                trigger=entry_price + distance,
                tif="GTC",
                reduce_only=True,
                tag="protective-stop",
            )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        previous_position = self._last_position
        self._last_position = fill.position

        if fill.position == 0:
            self.entry_price = None
            self.entry_atr = None
            self.pending_atr = None
            self._closing_for_session = False
            # If a manual/session-end close was the exit, any still-working
            # protective stop is no longer needed and would only generate a
            # lifecycle rejection when the flat position is evaluated.
            for order in ctx.find_orders(tag="protective-stop"):
                ctx.cancel(order.id)
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
        # A new session resets the session-end close gate. If a close order did
        # not fill before the previous close, we want normal management to
        # resume rather than staying blocked.
        if ctx.event.kind == "session_open":
            self._closing_for_session = False
