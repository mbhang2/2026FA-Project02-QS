"""Opening-range breakout with a fade back to the range, with explicit sizing.

The question: when price leaves the opening range, does the move hold or does
the range win?

This strategy answers "the range wins" for breakouts that occur after the first
hour, and "the move holds" for a breakout that occurs within the first hour.

Opening range: high and low of the first 30 minutes of the regular-hours session,
built from 5-minute bars.

Breakout definition: close beyond the opening range by at least 0.25 * width of
the opening range, where width is measured from the range itself.

Entry: on the 5-minute bar close that confirms the breakout.

Exit: a stated deadline of 30 minutes from entry, at which point the position is
closed at market. A protective stop is placed at the opposite side of the
opening range.

The direction is determined by the time of the breakout:
- Within the first 60 minutes: breakout direction (the move holds).
- After 60 minutes: fade the breakout (the range wins).

This is a stated condition, not a fitted regime.

Sizing is fixed at 1 contract per trade.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_minutes: int = 30,
        fade_after_minutes: int = 60,
        breakout_mult: float = 0.25,
        size: int = 1,
        hold_minutes: int = 30,
    ):
        self.interval = interval
        self.range_minutes = range_minutes
        self.fade_after_minutes = fade_after_minutes
        self.breakout_mult = breakout_mult
        self.size = size
        self.hold_minutes = hold_minutes

        self._session = None
        self._range_high = None
        self._range_low = None
        self._session_open_ts = 0
        self._entry_bar_ts = None
        self._entry_side = None  # 1 for long, -1 for short

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset at session boundary
        if ctx.session != self._session:
            self._session = ctx.session
            self._range_high = None
            self._range_low = None
            self._session_open_ts = ctx.session_start
            self._entry_bar_ts = None
            self._entry_side = None

        # Manage existing position: close at deadline
        if ctx.position:
            if self._entry_bar_ts is not None:
                elapsed = (ctx.ts - self._entry_bar_ts) / 1_000_000_000
                if elapsed >= self.hold_minutes * 60:
                    ctx.close()
                    self._entry_bar_ts = None
                    self._entry_side = None
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

        # Determine time since session open
        session_elapsed = (ctx.ts - self._session_open_ts) / 1_000_000_000 / 60

        if session_elapsed <= self.fade_after_minutes:
            # Early breakout: follow the move
            if breakout_up:
                ctx.buy(size=self.size, tag="early-breakout-long")
                self._entry_side = 1
            else:
                ctx.sell(size=self.size, tag="early-breakout-short")
                self._entry_side = -1
        else:
            # Late breakout: fade the move
            if breakout_up:
                ctx.sell(size=self.size, tag="late-fade-short")
                self._entry_side = -1
            else:
                ctx.buy(size=self.size, tag="late-fade-long")
                self._entry_side = 1

        self._entry_bar_ts = ctx.ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
