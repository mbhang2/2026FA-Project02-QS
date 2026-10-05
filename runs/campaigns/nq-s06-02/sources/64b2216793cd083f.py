"""Opening-range fade with volatility floor and breakout distance scaled by the
range itself, but only after the first hour of the session has passed.

The question: when price leaves the opening range, does the move hold or does
the range win? This strategy answers "the range wins" for breakouts that are
large relative to the opening range itself, and acts only after the first hour
of the regular-hours session has completed.

Opening range: high and low of the first 30 minutes of the regular-hours
session, tracked incrementally from 5-minute bars as they arrive.

Breakout definition: a 5-minute bar close beyond the opening range by at least
0.5 * the width of the opening range itself.

Direction: always fade the breakout. A close above the range is sold; a close
below is bought.

Entry: on the bar close that confirms the breakout, provided the bar's open
time is at least 60 minutes after the session open.

Exit: a stated deadline of 30 minutes from entry, at which point the position
is closed at market. A protective stop is placed one range-width beyond the
entry, in the direction of the breakout.

The rule acts on every session where a qualifying breakout occurs after the
first hour.

This version avoids the WindowTooShort crash by never scanning the full
history: the opening range is accumulated from bars as they close, and the
breakout check uses only the most recent bar.
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
        min_range_width: float = 0.5,
        entry_delay_minutes: int = 60,
    ):
        self.interval = interval
        self.range_minutes = range_minutes
        self.breakout_mult = breakout_mult
        self.size = size
        self.hold_minutes = hold_minutes
        self.stop_mult = stop_mult
        self.min_range_width = min_range_width
        self.entry_delay_minutes = entry_delay_minutes

        self._session = None
        self._session_open_ts = None
        self._range_high = None
        self._range_low = None
        self._entry_ts = None

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return

        # Session boundary: reset state
        if ctx.session != self._session:
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

        # Opening range is complete; require it to be non-degenerate and wide
        # enough to trade.
        if self._range_high is None or self._range_low is None:
            return
        range_width = self._range_high - self._range_low
        if range_width < self.min_range_width:
            return

        # Only act after the entry delay has passed
        entry_earliest_ts = self._session_open_ts + self.entry_delay_minutes * 60 * 1_000_000_000
        if bar.open_ts < entry_earliest_ts:
            return

        last = bar
        breakout_distance = self.breakout_mult * range_width
        breakout_up = last.close > self._range_high + breakout_distance
        breakout_down = last.close < self._range_low - breakout_distance

        if not (breakout_up or breakout_down):
            return

        stop_distance = self.stop_mult * range_width

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
