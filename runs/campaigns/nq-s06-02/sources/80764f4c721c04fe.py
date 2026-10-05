"""Opening-range breakout fade, but only in the direction opposite to a
strong overnight gap.

The question asks whether the move holds or the range wins. Prior fade
attempts have mostly lost money, while continuation attempts have been mixed.
This strategy conditions the fade on a stated feature: the overnight gap
between the prior session's close and the current session's open.

Mechanism: a large overnight gap means the opening range is likely to be
repriced information. When price then breaks out of the opening range in the
opposite direction of the gap, that breakout is likely to be a fade of the
gap itself, and the range wins. When the breakout is in the same direction as
the gap, the move is more likely to hold, so we do not trade it.

Opening range: high and low of the first 30 minutes of the regular-hours
session, tracked incrementally from 5-minute bars.

Breakout definition: a 5-minute bar close beyond the opening range by at
least 0.1 * the average true range of the prior 10 sessions.

Direction: fade only the breakout that goes against the overnight gap. A
close above the range is sold if the gap was down; a close below is bought if
the gap was up.

Entry: on the bar close that confirms the breakout.

Exit: a stated deadline of 10 minutes from entry, at which point the position
is closed at market. A protective stop is placed 0.1 average true ranges
beyond the entry, against the fade direction.

The rule acts on every session where a qualifying breakout occurs.

State is kept on self to avoid WindowTooShort: the opening range is
accumulated from bars as they close, and prior average true ranges are stored
in a rolling list.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_minutes: int = 30,
        breakout_mult: float = 0.1,
        size: int = 1,
        hold_minutes: int = 10,
        stop_mult: float = 0.1,
        lookback_sessions: int = 10,
        gap_mult: float = 0.5,
    ):
        self.interval = interval
        self.range_minutes = range_minutes
        self.breakout_mult = breakout_mult
        self.size = size
        self.hold_minutes = hold_minutes
        self.stop_mult = stop_mult
        self.lookback_sessions = lookback_sessions
        self.gap_mult = gap_mult

        self._session = None
        self._session_open_ts = None
        self._range_high = None
        self._range_low = None
        self._entry_ts = None
        self._prior_atrs: list[float] = []
        self._current_session_high = None
        self._current_session_low = None
        self._current_session_close = None
        self._prev_session_close = None
        self._session_open_price = None
        self._gap = None

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return

        # Session boundary: reset state and store the prior session's average
        # true range and close.
        if ctx.session != self._session:
            if self._session is not None:
                if (
                    self._current_session_high is not None
                    and self._current_session_low is not None
                    and self._current_session_close is not None
                ):
                    prev_close = self._current_session_close
                    true_range = max(
                        self._current_session_high - self._current_session_low,
                        abs(self._current_session_high - prev_close),
                        abs(self._current_session_low - prev_close),
                    )
                    self._prior_atrs.append(true_range)
                    if len(self._prior_atrs) > self.lookback_sessions:
                        self._prior_atrs.pop(0)
                    self._prev_session_close = prev_close

            self._session = ctx.session
            self._session_open_ts = ctx.session_start
            self._range_high = None
            self._range_low = None
            self._entry_ts = None
            self._current_session_high = None
            self._current_session_low = None
            self._current_session_close = None
            self._session_open_price = None
            self._gap = None

        # Track the session's high, low, and close from all bars.
        for bar in ctx.closed:
            if self._current_session_high is None:
                self._current_session_high = bar.high
                self._current_session_low = bar.low
            else:
                self._current_session_high = max(self._current_session_high, bar.high)
                self._current_session_low = min(self._current_session_low, bar.low)
            self._current_session_close = bar.close
            if self._session_open_price is None:
                self._session_open_price = bar.open

        # Compute the overnight gap once we have the session open price and
        # the previous session close.
        if self._gap is None and self._session_open_price is not None and self._prev_session_close is not None:
            self._gap = self._session_open_price - self._prev_session_close

        # Manage existing position: close at deadline
        if ctx.position:
            if self._entry_ts is not None:
                elapsed = (ctx.ts - self._entry_ts) / 1_000_000_000
                if elapsed >= self.hold_minutes * 60:
                    ctx.close()
                    self._entry_ts = None
            return

        # No position: collect opening range and look for breakout
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        range_end_ts = self._session_open_ts + self.range_minutes * 60 * 1_000_000_000

        # Accumulate opening range from bars that start within the first
        # range_minutes of the session.
        if bar.open_ts < range_end_ts:
            if self._range_high is None:
                self._range_high = bar.high
                self._range_low = bar.low
            else:
                self._range_high = max(self._range_high, bar.high)
                self._range_low = min(self._range_low, bar.low)
            return

        # Opening range is complete; require it to be non-degenerate
        if self._range_high is None or self._range_low is None:
            return
        range_width = self._range_high - self._range_low
        if range_width <= 0:
            return

        # Compute breakout threshold from recent average true ranges
        if len(self._prior_atrs) >= self.lookback_sessions:
            avg_atr = sum(self._prior_atrs) / len(self._prior_atrs)
            threshold = self.breakout_mult * avg_atr
            stop_distance = self.stop_mult * avg_atr
        else:
            threshold = self.breakout_mult * range_width
            stop_distance = self.stop_mult * range_width

        last = bar
        breakout_up = last.close > self._range_high + threshold
        breakout_down = last.close < self._range_low - threshold

        if not (breakout_up or breakout_down):
            return

        # Require a meaningful overnight gap to condition on.
        if self._gap is None or abs(self._gap) < self.gap_mult * avg_atr:
            return

        # Fade only the breakout that goes against the overnight gap.
        if breakout_up and self._gap < 0:
            ctx.sell(
                size=self.size,
                stop_offset=stop_distance,
                tag="range-fade-short-gap-down",
            )
            self._entry_ts = ctx.ts
        elif breakout_down and self._gap > 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_distance,
                tag="range-fade-long-gap-up",
            )
            self._entry_ts = ctx.ts

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
