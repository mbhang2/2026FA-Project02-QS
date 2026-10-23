"""Strategy #24: #18 plus session-end risk controls.

#18 is the campaign's best so far. It enters delta-confirmed three-bar
breakouts, size=3, with a protective stop placed on fill and a chandelier
trail. The one structural risk it has not addressed is the session boundary:
a position still open late in a session either has to be held through the
close or exits on a trailing stop that may lag a late reversal.

This version keeps #18's entry, ATR, stop and trail logic exactly, and adds
two boundary rules:

1. No new entries in the final `no_entry_margin_ns` (default 15 minutes) of a
   session. Late breakouts in thin end-of-session flow are the weakest signals.
2. Flatten any open position in the final `exit_margin_ns` (default 10
   minutes). The protective stop is left in place until the close fills, so
   the position is never naked; if the close fills, `on_fill` cancels the
   now-unneeded stop.

Both margins are measured against `ctx.session_end`, which is schedule
information and not lookahead.
"""

from __future__ import annotations

_NS_PER_SEC = 1_000_000_000


class Strategy:
    """Delta-confirmed three-bar breakout, chandelier trailing, session-flat."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 20,
        breakout_bars: int = 3,
        stop_mult: float = 0.90,
        breakeven_atr_mult: float = 0.35,
        trail_atr_mult: float = 0.75,
        trail_atr_distance_mult: float = 0.50,
        size: int = 3,
        no_entry_margin_ns: int = 15 * 60 * _NS_PER_SEC,
        exit_margin_ns: int = 10 * 60 * _NS_PER_SEC,
    ):
        self.interval = interval
        self.lookback = lookback
        self.breakout_bars = breakout_bars
        self.stop_mult = stop_mult
        self.breakeven_atr_mult = breakeven_atr_mult
        self.trail_atr_mult = trail_atr_mult
        self.trail_atr_distance_mult = trail_atr_distance_mult
        self.size = size
        self.no_entry_margin_ns = no_entry_margin_ns
        self.exit_margin_ns = exit_margin_ns

        self.entry_price: float | None = None
        self.entry_atr: float | None = None
        self.pending_atr: float | None = None
        self._last_position = 0
        self._session: int | None = None
        self._session_close_queued = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _average_true_range(self, window) -> float | None:
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

        if ctx.session != self._session:
            self._session = ctx.session
            self._session_close_queued = False

        self._last_position = ctx.position

        if ctx.position:
            # Near the close, start flattening instead of continuing to trail.
            if ctx.session_end - ctx.ts < self.exit_margin_ns:
                if not self._session_close_queued:
                    self._session_close_queued = True
                    self._exit_at_session_close(ctx)
                return
            self._manage_open_position(ctx)
            return

        if ctx.working_order_ids:
            return

        # Do not open a new position too close to the session end.
        if ctx.session_end - ctx.ts < self.no_entry_margin_ns:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        window = history.last(self.lookback + 1)
        if len(window) < self.lookback + 1:
            return
        if len(window) < self.breakout_bars + 1:
            return

        last = window[-1]
        prior = window[:-1]

        trend = sum(b.close for b in prior) / self.lookback

        # Pre-breakout ATR only, as in #18.
        atr_value = self._average_true_range(prior)
        if atr_value is None or atr_value <= 0:
            return
        atr = atr_value

        stop = atr * self.stop_mult
        if stop <= 0:
            return

        recent = window[-self.breakout_bars - 1:-1]
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

    def _exit_at_session_close(self, ctx) -> None:
        # The protective stop stays in place until the flattening close fills,
        # so the position is never left unprotected. If the close fills first,
        # on_fill cancels the now-unneeded stop.
        ctx.close()

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

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        previous_position = self._last_position
        self._last_position = fill.position

        if fill.position == 0:
            self.entry_price = None
            self.entry_atr = None
            self.pending_atr = None
            self._session_close_queued = False

            # If a session-close market order flattened us, a GTC protective
            # stop may still be resting. It is reduce-only and no longer useful.
            for stop in ctx.find_orders(tag="protective-stop"):
                ctx.cancel(stop.id)
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
            if stop_order.size < abs(fill.position):
                ctx.modify(stop_order.id, size=abs(fill.position))

    def on_lifecycle(self, ctx) -> None:
        pass
