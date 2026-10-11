"""Opening-range fade for NQ futures.

The campaign question is whether a move outside the regular-hours opening range
holds or the range wins. This strategy bets that the range wins: it fades the
first one-minute close outside the 30-minute regular-hours opening range.

The regular-hours open for the CME Globex session is 15.5 hours after
ctx.session_start (17:00 CT -> 8:30 CT). The opening range is built from
one-minute bars, and the strategy acts once per session on the first close
outside that range. It is flattened before the session close.
"""

from __future__ import annotations

from typing import Optional

from src.core.types import NS_PER_SECOND


class Strategy:
    def __init__(
        self,
        *,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 1.0,
        or_minutes: int = 30,
        entry_window_minutes: int = 240,
        exit_margin_minutes: int = 10,
    ):
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.or_minutes = or_minutes
        self.entry_window_minutes = entry_window_minutes
        self.exit_margin_minutes = exit_margin_minutes

        self._session: Optional[int] = None
        self._or_high: Optional[float] = None
        self._or_low: Optional[float] = None
        self._range_complete: bool = False
        self._traded_this_session: bool = False
        self._exit_ordered: bool = False
        self._rth_open_ts: Optional[int] = None
        self._or_end_ts: Optional[int] = None
        self._entry_deadline_ts: Optional[int] = None
        self._exit_ts: Optional[int] = None

    def setup(self, ctx) -> None:
        # One-minute bars with empty intervals emitted so the strategy gets a
        # callback every minute and can flatten near the session close even if
        # no trade printed.
        ctx.subscribe("1m", emit_empty=True)

    def on_bars(self, ctx) -> None:
        if "1m" not in ctx.bars:
            return
        bar = ctx.bars["1m"]

        if ctx.session != self._session:
            self._session = ctx.session
            self._or_high = None
            self._or_low = None
            self._range_complete = False
            self._traded_this_session = False
            self._exit_ordered = False

            # 17:00 CT previous day -> 8:30 CT regular-hours open
            rth_open_offset_seconds = 15 * 3600 + 30 * 60
            self._rth_open_ts = ctx.session_start + rth_open_offset_seconds * NS_PER_SECOND
            self._or_end_ts = self._rth_open_ts + self.or_minutes * 60 * NS_PER_SECOND
            self._entry_deadline_ts = self._rth_open_ts + (
                self.entry_window_minutes * 60 * NS_PER_SECOND
            )
            self._exit_ts = ctx.session_end - self.exit_margin_minutes * 60 * NS_PER_SECOND

        rth_open_ts = self._rth_open_ts
        or_end_ts = self._or_end_ts
        entry_deadline_ts = self._entry_deadline_ts
        exit_ts = self._exit_ts

        if rth_open_ts is None or or_end_ts is None:
            return
        if entry_deadline_ts is None or exit_ts is None:
            return

        # Mandatory flatten near session close. Cancel bracket legs first, then
        # market-close any remaining position.
        if (
            ctx.actionable
            and ctx.position
            and not self._exit_ordered
            and ctx.ts >= exit_ts
        ):
            ctx.cancel_all()
            ctx.close()
            self._exit_ordered = True
            return

        # Build the opening range from one-minute bars between RTH open and
        # RTH open + or_minutes.
        if bar.close_ts < rth_open_ts:
            return

        if bar.close_ts <= or_end_ts:
            or_high = self._or_high
            or_low = self._or_low

            if or_high is None:
                or_high = bar.high
            elif bar.high > or_high:
                or_high = bar.high

            if or_low is None:
                or_low = bar.low
            elif bar.low < or_low:
                or_low = bar.low

            self._or_high = or_high
            self._or_low = or_low

            if bar.close_ts >= or_end_ts:
                self._range_complete = True
            return

        self._range_complete = True

        if not ctx.actionable or self._traded_this_session:
            return
        if ctx.position or ctx.working_order_ids:
            return
        if bar.tick_count == 0:
            return
        if ctx.ts > entry_deadline_ts:
            return

        or_high = self._or_high
        or_low = self._or_low
        if or_high is None:
            return
        if or_low is None:
            return

        rng = or_high - or_low
        if rng <= 0:
            return

        stop_dist = rng * self.stop_mult
        target_dist = rng * self.target_mult

        # Fade the first close outside the opening range.
        if bar.close > or_high:
            oid = ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-fade-short",
            )
            if oid is not None:
                self._traded_this_session = True
        elif bar.close < or_low:
            oid = ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="or-fade-long",
            )
            if oid is not None:
                self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
