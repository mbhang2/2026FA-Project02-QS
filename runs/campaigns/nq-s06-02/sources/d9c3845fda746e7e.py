"""Opening-range breakout continuation with volatility-scaled threshold and
time-stop exit, using a smaller threshold and a longer hold to capture the
move when it holds.

The question asks whether the move holds or the range wins. Previous fade
attempts have all lost money, suggesting the move may hold more often than the
range wins. This strategy answers "the move holds" by trading breakouts in the
direction of the breakout.

Opening range: high and low of the first 30 minutes of the regular-hours
session, tracked incrementally from 5-minute bars.

Breakout definition: a 5-minute bar close beyond the opening range by at least
0.2 * the average true range of the prior 10 sessions. This is a smaller
threshold than the original, producing more trades.

Direction: always follow the breakout. A close above the range is bought; a
close below is sold.

Entry: on the bar close that confirms the breakout.

Exit: a stated deadline of 20 minutes from entry, at which point the position
is closed at market. A protective stop is placed 0.2 average true ranges
beyond the entry, against the breakout direction.

The rule acts on every session where a qualifying breakout occurs.

State is kept on self to avoid WindowTooShort: the opening range is accumulated
from bars as they close, and prior average true ranges are stored in a rolling
list.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_minutes: int = 30,
        breakout_mult: float = 0.2,
        size: int = 1,
        hold_minutes: int = 20,
        stop_mult: float = 0.2,
        lookback_sessions: int = 10,
    ):
        self.interval = interval
        self.range_minutes = range_minutes
        self.breakout_mult = breakout_mult
        self.size = size
        self.hold_minutes = hold_minutes
        self.stop_mult = stop_mult
        self.lookback_sessions = lookback_sessions

        self._session = None
        self._session_open_ts = None
        self._range_high = None
        self._range_low = None
        self._entry_ts = None
        self._prior_atrs: list[float] = []
        self._current_session_high = None
        self._current_session_low = None
        self._current_session_close = None

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return

        # Session boundary: reset state and store the prior session's average
        # true range.
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

            self._session = ctx.session
            self._session_open_ts = ctx.session_start
            self._range_high = None
            self._range_low = None
            self._entry_ts = None
            self._current_session_high = None
            self._current_session_low = None
            self._current_session_close = None

        # Track the session's high, low, and close from all bars.
        for bar in ctx.closed:
            if self._current_session_high is None:
                self._current_session_high = bar.high
                self._current_session_low = bar.low
            else:
                self._current_session_high = max(self._current_session_high, bar.high)
                self._current_session_low = min(self._current_session_low, bar.low)
            self._current_session_close = bar.close

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

        # Always follow the breakout, with a protective stop against the entry
        if breakout_up:
            stop_price = last.close - stop_distance
            ctx.buy(
                size=self.size,
                stop=stop_price,
                tag="range-breakout-long",
            )
        else:
            stop_price = last.close + stop_distance
            ctx.sell(
                size=self.size,
                stop=stop_price,
                tag="range-breakout-short",
            )

        self._entry_ts = ctx.ts

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
