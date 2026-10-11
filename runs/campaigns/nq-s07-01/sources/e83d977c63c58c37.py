"""Opening-range breakout with a 0.5r trailing stop and a 0.75r target.

The best prior strategy (#23) traded the first delta-confirmed 5-minute close
beyond a 30-minute RTH opening range, buffered by 10% of the range height, with
a symmetric half-range bracket.  It scored +0.1290.  The entry logic that
produced that result is kept intact here.

The exit is changed to let winners run a little further without giving back the
opening move.  The initial protective stop is half the opening-range height.
Once price moves in the trade's favor, the stop is trailed at half the range
height behind the best price since entry.  The profit target is set to 0.75
times the range height.  For a trade that reaches the old half-range target,
the trailing stop is then at breakeven, so the trade either captures 0.75r or
exits near flat instead of turning a winner into a loser.  The stop is never
moved down for a long or up for a short.

Opening range:
    High and low of the first 30 minutes of regular-hours trading
    (`08:30 CT` to `09:00 CT`), measured on 5-minute bars.  The RTH open is
    15.5 hours after the CME Globex session open (`17:00 CT`); with bars
    anchored to the session open this is a fixed bar offset and involves no
    lookahead.

Signal:
    After the range is known, the first completed 5-minute bar that closes
    above the range high by at least `breakout_buffer_mult` times the range
    height with `bar.delta > 0` is bought.  The first completed bar that closes
    below the range low by at least the same buffer with `bar.delta < 0` is
    sold.  One trade per session.

Exit:
    Entry is protected immediately by the engine's bracket: the initial stop is
    half the opening-range height and the target is 0.75 times the range
    height.  On subsequent 5-minute bars the stop is trailed by half the range
    height behind the best price since entry, never moved against the trade.
    Any open position is flattened by 15:00 CT or earlier on short sessions.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND, Side


class Strategy:
    """Trade the first delta-confirmed opening-range breakout, then trail the
    protective stop at half the range height behind the best price."""

    # Seconds from the CME Globex session open (17:00 CT) to the RTH open
    # (08:30 CT next day).  17:00 -> 08:30 = 15h30m.
    RTH_OPEN_DELAY_SEC = 15 * 3600 + 30 * 60

    # The opening range is the first 30 minutes of RTH.
    OPENING_RANGE_SEC = 30 * 60

    # Flatten by 15:00 CT, giving a full hour before the 16:00 CT session close.
    EXIT_DELAY_SEC = 22 * 3600

    # Do not open a new trade in the last 30 minutes before the scheduled exit.
    ENTRY_CUTOFF_DELAY_SEC = EXIT_DELAY_SEC - 30 * 60

    # If a session ends early (half-day), force the exit this long before end.
    SESSION_EXIT_MARGIN_SEC = 10 * 60

    def __init__(
        self,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 0.75,
        trail_mult: float = 0.5,
        min_range: float = 2.0,
        breakout_buffer_mult: float = 0.1,
    ):
        self.interval = "5m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.trail_mult = trail_mult
        self.min_range = min_range
        self.breakout_buffer_mult = breakout_buffer_mult

        self._session: int = -1
        self._rth_open_ts: int = 0
        self._or_end_ts: int = 0
        self._entry_cutoff_ts: int = 0
        self._exit_ts: int = 0
        self._or_high: Optional[float] = None
        self._or_low: Optional[float] = None
        self._range_complete: bool = False
        self._traded_this_session: bool = False
        self._exit_ordered: bool = False

        # Trailing-stop state.
        self._entry_id: Optional[int] = None
        self._entry_price: Optional[float] = None
        self._entry_side: int = 0  # +1 long, -1 short, 0 no active entry
        self._peak: Optional[float] = None
        self._stop_level: Optional[float] = None
        self._stop_dist: float = 0.0
        self._trail_dist: float = 0.0

    def setup(self, ctx) -> None:
        # Guarantee a callback every 5 minutes even when no trade occurs, so
        # the range is measured on time and positions can be flattened near
        # the session close even on thin sessions.
        ctx.subscribe(self.interval, emit_empty=True)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # New session: reset session-local state and precompute fixed times.
        if ctx.session != self._session:
            self._session = ctx.session
            self._rth_open_ts = (
                ctx.session_start + self.RTH_OPEN_DELAY_SEC * NS_PER_SECOND
            )
            self._or_end_ts = (
                self._rth_open_ts + self.OPENING_RANGE_SEC * NS_PER_SECOND
            )
            self._entry_cutoff_ts = (
                ctx.session_start + self.ENTRY_CUTOFF_DELAY_SEC * NS_PER_SECOND
            )
            self._exit_ts = min(
                ctx.session_start + self.EXIT_DELAY_SEC * NS_PER_SECOND,
                ctx.session_end - self.SESSION_EXIT_MARGIN_SEC * NS_PER_SECOND,
            )
            self._or_high = None
            self._or_low = None
            self._range_complete = False
            self._traded_this_session = False
            self._exit_ordered = False
            self._entry_id = None
            self._entry_price = None
            self._entry_side = 0
            self._peak = None
            self._stop_level = None
            self._stop_dist = 0.0
            self._trail_dist = 0.0

        # Build the opening range from bars between RTH open and RTH open + 30m.
        if not self._range_complete:
            if bar.close_ts <= self._rth_open_ts:
                return

            if bar.open_ts < self._or_end_ts:
                if bar.tick_count > 0:
                    high = bar.high
                    low = bar.low
                    if self._or_high is None:
                        self._or_high = high
                    elif high > self._or_high:
                        self._or_high = high
                    if self._or_low is None:
                        self._or_low = low
                    elif low < self._or_low:
                        self._or_low = low

                if bar.close_ts >= self._or_end_ts:
                    self._range_complete = True
                # Never act on a bar that is part of the range definition.
                return

            # First bar whose open is at/after the range end: range is complete.
            self._range_complete = True

        if not ctx.actionable:
            return

        # Manage an open position: mandatory flatten near the close, and
        # trailing the protective stop outside of the flatten bar.
        if ctx.position:
            if bar.close_ts >= self._exit_ts and not self._exit_ordered:
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True
            elif not self._exit_ordered:
                self._maybe_trail(ctx)
            return

        if self._traded_this_session:
            return
        if bar.close_ts >= self._entry_cutoff_ts:
            return
        if bar.tick_count == 0:
            return

        or_high = self._or_high
        or_low = self._or_low
        if or_high is None or or_low is None:
            return

        rng = or_high - or_low
        if rng < self.min_range:
            return

        buffer = rng * self.breakout_buffer_mult
        stop_dist = rng * self.stop_mult
        trail_dist = rng * self.trail_mult
        target_dist = rng * self.target_mult

        order_id = None

        # Trade the first close outside the range by at least the buffer whose
        # signed order flow confirms the direction of the break.
        if bar.close > or_high + buffer and bar.delta > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-trail75-long",
            )
        elif bar.close < or_low - buffer and bar.delta < 0:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-trail75-short",
            )

        if order_id is not None:
            self._traded_this_session = True
            self._entry_id = order_id
            self._entry_price = None
            self._entry_side = 0
            self._peak = None
            self._stop_level = None
            self._stop_dist = stop_dist
            self._trail_dist = trail_dist

    def _maybe_trail(self, ctx) -> None:
        """Trail the protective stop behind the best price since entry."""
        if (
            self._entry_id is None
            or self._entry_price is None
            or self._entry_side == 0
            or self._peak is None
            or self._stop_level is None
        ):
            return

        last = ctx.last_price

        if self._entry_side > 0:
            if last > self._peak:
                self._peak = last
            new_stop = self._peak - self._trail_dist
            # Never move a long stop down.
            if new_stop <= self._stop_level:
                return
            stops = ctx.find_orders(parent_id=self._entry_id, side=Side.SELL)
            stop_order = next(
                (o for o in stops if getattr(o, "stop_price", None) is not None),
                None,
            )
            if stop_order is not None:
                ctx.modify(stop_order.id, trigger=new_stop)
                self._stop_level = new_stop
        else:
            if last < self._peak:
                self._peak = last
            new_stop = self._peak + self._trail_dist
            # Never move a short stop up.
            if new_stop >= self._stop_level:
                return
            stops = ctx.find_orders(parent_id=self._entry_id, side=Side.BUY)
            stop_order = next(
                (o for o in stops if getattr(o, "stop_price", None) is not None),
                None,
            )
            if stop_order is not None:
                ctx.modify(stop_order.id, trigger=new_stop)
                self._stop_level = new_stop

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            # The position was closed by stop, target, or the time exit.
            self._entry_id = None
            self._entry_price = None
            self._entry_side = 0
            self._peak = None
            self._stop_level = None
            return

        # Initial entry fill only: record the price and side for trailing.
        if self._entry_id is not None and self._entry_price is None:
            self._entry_price = fill.price
            self._entry_side = 1 if fill.position > 0 else -1
            self._peak = fill.price
            if self._entry_side > 0:
                self._stop_level = fill.price - self._stop_dist
            else:
                self._stop_level = fill.price + self._stop_dist

    def on_lifecycle(self, ctx) -> None:
        pass
