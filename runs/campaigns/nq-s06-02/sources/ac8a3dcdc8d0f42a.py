"""Opening-range fade with a hard deadline and a protective stop at the range edge.

The question: when price leaves the opening range, does the move hold or does
the range win? This strategy answers "the range wins" for all qualifying
breakouts, acting on every session where one occurs.

Opening range: high and low of the first 30 minutes of the regular-hours session.

Breakout definition: a 5-minute bar close beyond the opening range by at least
0.25 * the width of the opening range itself. The width is measured from the
range, as the campaign asks.

Direction: always fade the breakout. A close above the range is sold; a close
below is bought. The range wins.

Entry: on the bar close that confirms the breakout.

Exit: a stated deadline of 30 minutes from entry, at which point the position is
closed at market. A protective stop is placed at the opposite edge of the
opening range, so the risk is the range width plus the breakout buffer.

The rule acts on every session where a qualifying breakout occurs.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_minutes: int = 30,
        breakout_mult: float = 0.25,
        size: int = 1,
        hold_minutes: int = 30,
    ):
        self.interval = interval
        self.range_minutes = range_minutes
        self.breakout_mult = breakout_mult
        self.size = size
        self.hold_minutes = hold_minutes

        self._session = None
        self._range_high = None
        self._range_low = None
        self._session_open_ts = None
        self._entry_ts = None

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return

        # Reset at session boundary
        if ctx.session != self._session:
            self._session = ctx.session
            self._range_high = None
            self._range_low = None
            self._session_open_ts = ctx.session_start
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
        history = ctx.history(self.interval)
        if not history:
            return

        # Build opening range from bars that start within the first range_minutes
        if self._range_high is None:
            range_end_ts = self._session_open_ts + self.range_minutes * 60 * 1_000_000_000
            range_bars = [b for b in history if b.open_ts < range_end_ts]
            if range_bars:
                self._range_high = max(b.high for b in range_bars)
                self._range_low = min(b.low for b in range_bars)

        if self._range_high is None or self._range_low is None:
            return

        range_width = self._range_high - self._range_low
        if range_width <= 0:
            return

        last = history[-1]
        breakout_distance = self.breakout_mult * range_width

        breakout_up = last.close > self._range_high + breakout_distance
        breakout_down = last.close < self._range_low - breakout_distance

        if not (breakout_up or breakout_down):
            return

        # Always fade the breakout, with a protective stop at the range edge
        if breakout_up:
            stop_price = self._range_low
            ctx.sell(
                size=self.size,
                stop=stop_price,
                tag="range-fade-short",
            )
        else:
            stop_price = self._range_high
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
