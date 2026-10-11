"""Opening-range breakout triggered on 1-minute bars, range measured on 5-minute bars.

The campaign asks whether a move out of the regular-hours opening range holds or
the range wins.  The best prior strategy (#23) bought the first 5-minute close
above the 30-minute RTH opening range by at least 10% of the range height whose
signed order flow (`bar.delta`) agreed with the break, sold the mirror image, and
protected the trade with a symmetric half-range bracket.  It scored +0.1290.

This proposal keeps #23's range construction, 10% buffer, delta confirmation,
and symmetric half-range bracket exactly, but moves the **trigger** to 1-minute
bars.  The opening range is still the first 30 minutes of RTH, measured from
5-minute bars, so the range itself is unchanged.  Once that range is known, the
first completed 1-minute bar that closes beyond the range by the buffer with
confirming delta is traded.

The claim is that #23 leaves the entry on the 5-minute clock.  A 5-minute bar
closing at 09:05 can be up to five minutes late to a 09:00 breakout; a 1-minute
trigger can act at 09:01.  The opening-range break is a short-horizon event, and
the extra four minutes of delay may be enough to pay a worse price and give back
part of the move.  The signal is otherwise identical, so the strategy stays
active on the ordinary run of sessions.

Opening range:
    High and low of the first 30 minutes of regular-hours trading
    (`08:30 CT` to `09:00 CT`), measured on 5-minute bars.  The RTH open is
    15.5 hours after the CME Globex session open (`17:00 CT`); with bars
    anchored to the session open this is a fixed bar offset and involves no
    lookahead.

Signal:
    After the range is known, the first completed 1-minute bar that closes above
    the range high by at least `breakout_buffer_mult` times range height, with
    `bar.delta > 0`, is bought.  The first completed 1-minute bar that closes
    below the range low by at least the same buffer, with `bar.delta < 0`, is
    sold.  One trade per session.

Exit:
    Entry is protected immediately by the engine's bracket, with stop and target
    offsets both equal to half the opening-range height.  Any open position is
    flattened by `15:00 CT`, or earlier on short sessions, because the engine
    does not flatten positions automatically.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    """Trade the first delta-confirmed 1-minute opening-range breakout."""

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
        target_mult: float = 0.5,
        min_range: float = 2.0,
        breakout_buffer_mult: float = 0.1,
    ):
        self.range_interval = "5m"
        self.trigger_interval = "1m"
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
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

    def setup(self, ctx) -> None:
        # 5-minute bars build the opening range and guarantee timely flatten
        # callbacks.  1-minute bars are the breakout trigger.
        ctx.subscribe(self.range_interval, emit_empty=True)
        ctx.subscribe(self.trigger_interval, emit_empty=True)

    def on_bars(self, ctx) -> None:
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

        # Build the opening range exclusively from 5-minute bars.  The range is
        # known when the 08:55-09:00 5-minute bar closes.
        if not self._range_complete:
            if self.range_interval not in ctx.bars:
                return
            range_bar = ctx.bars[self.range_interval]

            if range_bar.close_ts <= self._rth_open_ts:
                return

            if range_bar.open_ts < self._or_end_ts:
                if range_bar.tick_count > 0:
                    high = range_bar.high
                    low = range_bar.low
                    if self._or_high is None:
                        self._or_high = high
                    elif high > self._or_high:
                        self._or_high = high
                    if self._or_low is None:
                        self._or_low = low
                    elif low < self._or_low:
                        self._or_low = low

                if range_bar.close_ts >= self._or_end_ts:
                    self._range_complete = True
                # Never act on a bar that is part of the range definition.
                return

            # First 5-minute bar whose open is at/after the range end.
            self._range_complete = True

        if not ctx.actionable:
            return

        # The breakout trigger is the 1-minute bar.  Ignore any 1-minute bar
        # that closes exactly at the opening-range end; it is still inside the
        # range interval.
        if self.trigger_interval not in ctx.bars:
            return
        bar = ctx.bars[self.trigger_interval]
        if bar.close_ts <= self._or_end_ts:
            return

        # Mandatory flatten before the session ends.  The engine's bracket
        # handles stop/target; this prevents an overnight hold.
        if ctx.position:
            if bar.close_ts >= self._exit_ts and not self._exit_ordered:
                ctx.cancel_all()
                close_id = ctx.close()
                if close_id is not None:
                    self._exit_ordered = True
            return

        if self._traded_this_session:
            return
        if bar.close_ts >= self._entry_cutoff_ts:
            return
        if bar.tick_count == 0 or bar.volume <= 0:
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
        target_dist = rng * self.target_mult

        order_id = None

        # Trade the first 1-minute close outside the range by at least the
        # buffer whose signed order flow confirms the direction of the break.
        if bar.close > or_high + buffer and bar.delta > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-1m-long",
            )
        elif bar.close < or_low - buffer and bar.delta < 0:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-1m-short",
            )

        if order_id is not None:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
