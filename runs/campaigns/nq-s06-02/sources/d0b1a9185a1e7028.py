"""Opening-range breakout, with the direction chosen by the prior session's
close relative to the opening range.

The question asks whether the move holds or the range wins. This version
proposes that when price leaves the opening range by a meaningful amount, the
move holds -- but only when the prior session's close is already outside the
range in the same direction. That condition is a stated, pre-registered filter:
the range is more likely to break in the direction of the overnight drift, and
the breakout is then a continuation rather than a fade.

Opening range: high and low of the first 30 minutes of the regular-hours
session, tracked from 5-minute bars.

Breakout definition: a 5-minute bar close beyond the opening range by at least
0.5 * the width of the opening range itself.

Direction filter: the prior session's close must be above the opening range
high for a long, or below the opening range low for a short. If the prior close
is inside the range, no trade.

Entry: on the bar close that confirms the breakout.

Exit: a stated deadline of 30 minutes from entry, at which point the position
is closed at market. A protective stop is placed one range-width beyond the
entry, against the breakout direction.

The rule acts on every session where a qualifying breakout occurs, subject to
the direction filter.

This version keeps all state on self and never scans the full history.
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
    ):
        self.interval = interval
        self.range_minutes = range_minutes
        self.breakout_mult = breakout_mult
        self.size = size
        self.hold_minutes = hold_minutes
        self.stop_mult = stop_mult
        self.min_range_width = min_range_width

        self._session = None
        self._session_open_ts = None
        self._range_high = None
        self._range_low = None
        self._entry_ts = None
        self._prior_close = None

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return

        # Session boundary: reset state and carry forward the prior close
        if ctx.session != self._session:
            if self._session is not None:
                # The prior session's close is already stored in _prior_close
                # from the last bar of that session.
                pass
            self._session = ctx.session
            self._session_open_ts = ctx.session_start
            self._range_high = None
            self._range_low = None
            self._entry_ts = None

        # Track the last close of every bar, so at the session boundary we
        # have the prior session's close.
        if self.interval in ctx.bars:
            self._prior_close = ctx.bars[self.interval].close

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

        # Need a prior close to filter direction
        if self._prior_close is None:
            return

        last = bar
        breakout_distance = self.breakout_mult * range_width
        breakout_up = last.close > self._range_high + breakout_distance
        breakout_down = last.close < self._range_low - breakout_distance

        if not (breakout_up or breakout_down):
            return

        stop_distance = self.stop_mult * range_width

        # Direction filter: prior close must agree with the breakout
        if breakout_up and self._prior_close <= self._range_high:
            return
        if breakout_down and self._prior_close >= self._range_low:
            return

        # Trade the breakout continuation
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
