"""Opening-range breakout with a stated deadline and a three-way exit.

The question asks: when price leaves the opening range, does the move hold or
does the range win?  This strategy takes the "move holds" side for a defined
breakout, but with explicit exits so the answer is readable.

Opening range: first 30 minutes of the regular session, built from 5-minute
bars.  Leaving is defined as a close beyond the range by at least 0.5 times the
range width.  Entry is on the next bar after the breakout close, with a bracket
and a hard deadline of 10 bars (50 minutes) from entry.  The exit decomposition
is tracked in tags: target hit, stopped out, or deadline expired.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_bars: int = 6,          # 30 minutes at 5m
        breakout_mult: float = 0.5,   # close must exceed range by this fraction
        hold_bars: int = 10,          # deadline after entry
        stop_mult: float = 1.0,       # stop distance as multiple of range width
        target_mult: float = 2.0,     # target distance as multiple of range width
        size: int = 1,
    ):
        self.interval = interval
        self.range_bars = range_bars
        self.breakout_mult = breakout_mult
        self.hold_bars = hold_bars
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size

        # per-session state
        self._session = None
        self._range_high = 0.0
        self._range_low = 0.0
        self._range_ready = False
        self._entry_bar_count = 0
        self._bars_since_entry = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # session change: reset opening range
        if ctx.session != self._session:
            self._session = ctx.session
            self._range_high = 0.0
            self._range_low = 0.0
            self._range_ready = False
            self._entry_bar_count = 0
            self._bars_since_entry = 0

        history = ctx.history(self.interval)
        if not history:
            return

        # build the opening range from the first `range_bars` bars of the session
        if not self._range_ready:
            if len(history) >= self.range_bars:
                first_bars = history.last(self.range_bars)
                self._range_high = max(b.high for b in first_bars)
                self._range_low = min(b.low for b in first_bars)
                self._range_ready = True
            else:
                return

        # we are in a position: manage the deadline
        if ctx.position:
            self._bars_since_entry += 1
            if self._bars_since_entry >= self.hold_bars:
                # deadline expired: flatten
                ctx.close(tag="deadline-exit")
            return

        # flat, no working orders: look for a breakout
        if ctx.working_order_ids:
            return

        last = history[-1]
        range_width = self._range_high - self._range_low
        if range_width <= 0:
            return

        # only act after the range is complete and we have at least one bar beyond it
        if len(history) <= self.range_bars:
            return

        # breakout condition: close beyond the range by a fraction of its width
        if last.close > self._range_high + self.breakout_mult * range_width:
            stop_dist = self.stop_mult * range_width
            target_dist = self.target_mult * range_width
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="breakout-long",
            )
            self._entry_bar_count = len(history)
            self._bars_since_entry = 0
        elif last.close < self._range_low - self.breakout_mult * range_width:
            stop_dist = self.stop_mult * range_width
            target_dist = self.target_mult * range_width
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="breakout-short",
            )
            self._entry_bar_count = len(history)
            self._bars_since_entry = 0

    def on_fill(self, ctx) -> None:
        # reset the bar counter when a new position is opened
        if ctx.fill.position != 0:
            self._bars_since_entry = 0

    def on_lifecycle(self, ctx) -> None:
        pass
