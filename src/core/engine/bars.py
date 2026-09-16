"""Time-bar aggregation, anchored to the session open.

Anchored to the session, not wall-clock: a 5m grid that starts at midnight UTC
puts the first bar of a session in the middle of an interval, and every bar after
it is offset from the thing a trader would call a five-minute bar.
"""

from __future__ import annotations

from ..types import Bar, Subscription, Tick, Ts


class BarBuilder:
    """One subscription's aggregation state for one session.

    Closure is triggered by the first tick with `ts >= boundary`; a bar's
    contents are the ticks in `[start, boundary)`, and the triggering tick starts
    the next bar. That asymmetry is the whole reason bars carry both timestamps.
    """

    __slots__ = (
        "sub",
        "_anchor",
        "_open_ts",
        "_o",
        "_h",
        "_l",
        "_c",
        "_vol",
        "_n",
        "_session",
        "_last_close",
    )

    def __init__(self, sub: Subscription):
        self.sub = sub
        self._anchor: Ts | None = None
        self._open_ts: Ts = 0
        self._o = self._h = self._l = self._c = 0.0
        self._vol = 0
        self._n = 0
        self._session = -1
        self._last_close: float | None = None

    # ---- session handling -------------------------------------------------- #
    def start_session(self, session: int, session_start: Ts) -> None:
        self._anchor = session_start
        self._session = session
        self._open_ts = session_start
        self._reset_accumulators()
        self._last_close = None

    def _reset_accumulators(self) -> None:
        self._o = self._h = self._l = self._c = 0.0
        self._vol = 0
        self._n = 0

    def _boundary_after(self, ts: Ts) -> Ts:
        assert self._anchor is not None
        elapsed = ts - self._anchor
        n = elapsed // self.sub.interval_ns
        return self._anchor + (n + 1) * self.sub.interval_ns

    @property
    def current_boundary(self) -> Ts:
        return self._open_ts + self.sub.interval_ns

    # ---- the walk ---------------------------------------------------------- #
    def bars_closed_by(self, tick: Tick) -> list[Bar]:
        """Bars that this tick closes, oldest first.

        A quiet stretch can close several intervals at once; each one is a
        separate bar, and only the intervals that actually held ticks survive
        unless `emit_empty` is set.
        """
        if self._anchor is None:
            return []
        out: list[Bar] = []
        while tick.ts >= self.current_boundary:
            bar = self._close_current()
            if bar is not None:
                out.append(bar)
            self._open_ts = self.current_boundary
            self._reset_accumulators()
        return out

    def add(self, tick: Tick) -> None:
        if self._n == 0:
            self._o = self._h = self._l = self._c = tick.price
        else:
            self._h = max(self._h, tick.price)
            self._l = min(self._l, tick.price)
            self._c = tick.price
        self._vol += tick.size
        self._n += 1

    def add_span(self, high: float, low: float, close: float, volume: int, count: int,
                 first: float) -> None:
        """Fold a whole run of ticks in at once.

        Only valid when the span crosses no boundary of this subscription, which
        is what the event walk guarantees before calling it. The reductions are
        the same ones `add` performs one tick at a time -- this exists because
        doing them one tick at a time in Python was most of the cost of a
        session.
        """
        if count <= 0:
            return
        if self._n == 0:
            self._o = first
            self._h, self._l = high, low
        else:
            self._h = max(self._h, high)
            self._l = min(self._l, low)
        self._c = close
        self._vol += volume
        self._n += count

    def _close_current(self, close_ts: Ts | None = None) -> Bar | None:
        close_ts = self.current_boundary if close_ts is None else close_ts
        if self._n == 0:
            # No ticks in the interval. Skipped by default; emit_empty carries
            # the previous close forward at zero volume for strategies that want
            # a regular grid. There is nothing to carry before the session's
            # first tick, so leading empties are skipped either way.
            if not self.sub.emit_empty or self._last_close is None:
                return None
            p = self._last_close
            return Bar(
                sub_id=self.sub.sub_id,
                open_ts=self._open_ts,
                close_ts=close_ts,
                open=p,
                high=p,
                low=p,
                close=p,
                volume=0,
                tick_count=0,
                session=self._session,
            )
        self._last_close = self._c
        return Bar(
            sub_id=self.sub.sub_id,
            open_ts=self._open_ts,
            close_ts=close_ts,
            open=self._o,
            high=self._h,
            low=self._l,
            close=self._c,
            volume=self._vol,
            tick_count=self._n,
            session=self._session,
        )

    def flush_session(self, close_ts: Ts | None = None) -> Bar | None:
        """The partial bar at a session close.

        Emitted so a session's last interval is not silently discarded; it is a
        real bar with fewer ticks, and dropping it would quietly shorten every
        session by up to one interval.

        **Stamped with the session's close, not the interval's.** A tail bar is
        truncated by definition, and stamping it at the boundary it never reached
        put a `close_ts` in the future on it: the bar was delivered at the
        session close and claimed to close up to a full interval later. Anything
        joining on `close_ts` would order it after data that genuinely came
        first, and a strategy told it is called at `close_ts` was being told
        something untrue -- about the one bar of the session where a position is
        most likely still open.

        Invisible until Task 17 because every synthetic session was an exact
        multiple of the bar interval, so there was no tail bar to mis-stamp.
        """
        if self._anchor is None or self._n == 0:
            return None
        if close_ts is not None and close_ts > self.current_boundary:
            # The session outlasts the interval, so the interval closed normally
            # and this is not a truncated bar.
            close_ts = self.current_boundary
        bar = self._close_current(close_ts)
        self._reset_accumulators()
        return bar
