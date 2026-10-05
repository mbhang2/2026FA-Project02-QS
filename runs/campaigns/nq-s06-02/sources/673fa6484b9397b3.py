"""Opening-range fade with volatility-scaled confirmation, hard deadline, and
an explicit time-of-day filter that restricts entries to the session's first
two hours after the opening range has completed.

The question: when price leaves the opening range, does the move hold or does
the range win? This strategy answers "the range wins" for breakouts that are
large relative to recent realized volatility, but only during the session's
early morning hours. The first version's profit concentrated in hours 14-17
UTC; this version deliberately restricts entries to the first two hours after
the open, when the opening-range mechanism is most directly observable. If the
effect is real, it should survive in the ordinary early session rather than
depending on late-day moves.

Opening range: high and low of the first 30 minutes of the regular-hours
session, tracked incrementally from 5-minute bars as they arrive.

Breakout definition: a 5-minute bar close beyond the opening range by at least
0.5 * the average opening-range width of the prior 10 sessions.

Direction: always fade the breakout. A close above the range is sold; a close
below is bought.

Entry: on the bar close that confirms the breakout, provided the bar's open
time is at least 60 minutes after the session open and no later than 120
minutes after the session open.

Exit: a stated deadline of 30 minutes from entry, at which point the position
is closed at market. A protective stop is placed one average-range width beyond
the entry, in the direction of the breakout.

The rule acts on every session where a qualifying breakout occurs within the
allowed window.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_minutes: int = 30,
        breakout_mult: float = 0.5,
        size: int = 1,
        hold_minutes: int = 30,
        stop_mult: float = 1.0,
        lookback_sessions: int = 10,
        entry_delay_minutes: int = 60,
        entry_cutoff_minutes: int = 120,
    ):
        self.interval = interval
        self.range_minutes = range_minutes
        self.breakout_mult = breakout_mult
        self.size = size
        self.hold_minutes = hold_minutes
        self.stop_mult = stop_mult
        self.lookback_sessions = lookback_sessions
        self.entry_delay_minutes = entry_delay_minutes
        self.entry_cutoff_minutes = entry_cutoff_minutes

        self._session = None
        self._session_open_ts = None
        self._range_high = None
        self._range_low = None
        self._entry_ts = None
        self._prior_range_widths: list[float] = []

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return

        # Session boundary: reset state and store the prior session's range width
        if ctx.session != self._session:
            if self._session is not None and self._range_high is not None:
                self._prior_range_widths.append(self._range_high - self._range_low)
                if len(self._prior_range_widths) > self.lookback_sessions:
                    self._prior_range_widths.pop(0)
            self._session = ctx.session
            self._session_open_ts = ctx.session_start
            self._range_high = None
            self._range_low = None
            self._entry_ts = None

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

        # Only act within the allowed entry window
        entry_earliest_ts = self._session_open_ts + self.entry_delay_minutes * 60 * 1_000_000_000
        entry_latest_ts = self._session_open_ts + self.entry_cutoff_minutes * 60 * 1_000_000_000
        if bar.open_ts < entry_earliest_ts or bar.open_ts >= entry_latest_ts:
            return

        # Compute breakout threshold from recent range widths
        if len(self._prior_range_widths) >= self.lookback_sessions:
            avg_range = sum(self._prior_range_widths) / len(self._prior_range_widths)
            threshold = self.breakout_mult * avg_range
            stop_distance = self.stop_mult * avg_range
        else:
            threshold = self.breakout_mult * range_width
            stop_distance = self.stop_mult * range_width

        last = bar
        breakout_up = last.close > self._range_high + threshold
        breakout_down = last.close < self._range_low - threshold

        if not (breakout_up or breakout_down):
            return

        # Always fade the breakout, with a protective stop beyond the entry
        if breakout_up:
            stop_price = last.close + stop_distance
            ctx.sell(
                size=self.size,
                stop=stop_price,
                tag="range-fade-short",
            )
        else:
            stop_price = last.close - stop_distance
            ctx.buy(
                size=self.size,
                stop=stop_price,
                tag="range-fade-long",
            )

        self._entry_ts = ctx.ts

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
