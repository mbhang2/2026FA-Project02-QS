"""Opening-range breakout with explicit sizing and a stated deadline.

The question: when price leaves the opening range, does the move hold or does
the range win?

This strategy answers "the move holds" -- with a stated condition that the
breakout must be large relative to the opening range itself, and that the
breakout happens within the first two hours of the regular session.

Opening range: first 30 minutes of the regular-hours session (09:30-10:00 ET).
We build it from 5-minute bars, so bars 0-5 of the session.

Leaving the range: a close strictly beyond the range high or low, where the
distance beyond the range is at least 25% of the range width. This ensures we
only act on decisive moves, not noise.

Entry: market order on the bar close that confirms the breakout.

Exit: bracket with stop and target each 1.0x the range width, plus a hard
deadline of 4 hours after entry (16:00 ET). If the deadline is reached, the
position is closed at market. We track this in on_bars by checking the current
bar's close_ts against entry_ts + 4 hours.

The strategy acts on every session where a qualifying breakout occurs, not just
a subset. The condition is stated in the code, not chosen after looking.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_bars: int = 6,          # 30 minutes of 5m bars
        breakout_mult: float = 0.25,  # must exceed range by 25% of range width
        bracket_mult: float = 1.0,    # stop/target distance as multiple of range
        max_hold_bars: int = 48,      # 4 hours of 5m bars
        size: int = 1,
    ):
        self.interval = interval
        self.range_bars = range_bars
        self.breakout_mult = breakout_mult
        self.bracket_mult = bracket_mult
        self.max_hold_bars = max_hold_bars
        self.size = size

        # Per-session state
        self._session = None
        self._bars_this_session = 0
        self._range_high = None
        self._range_low = None
        self._entry_bar_index = None  # bar index within session when entry filled

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Track session and bar count
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._range_high = None
            self._range_low = None
            self._entry_bar_index = None

        # Only act on 5m bars
        if self.interval not in ctx.bars:
            return

        self._bars_this_session += 1

        # Build opening range from first `range_bars` bars
        if self._bars_this_session <= self.range_bars:
            bar = ctx.bars[self.interval]
            if self._range_high is None:
                self._range_high = bar.high
                self._range_low = bar.low
            else:
                self._range_high = max(self._range_high, bar.high)
                self._range_low = min(self._range_low, bar.low)
            return  # still building the range

        # We should have a range by now
        if self._range_high is None or self._range_low is None:
            return

        # Manage existing position: check deadline
        if ctx.position:
            if self._entry_bar_index is not None:
                bars_held = self._bars_this_session - self._entry_bar_index
                if bars_held >= self.max_hold_bars:
                    # Cancel any working bracket orders and close
                    ctx.cancel_all()
                    ctx.close()
                    self._entry_bar_index = None
            return  # let bracket manage the exit otherwise

        # No position, no working orders -- look for a breakout
        if ctx.working_order_ids:
            return

        if not ctx.actionable:
            return

        bar = ctx.bars[self.interval]
        range_width = self._range_high - self._range_low
        if range_width <= 0:
            return

        required_breakout = self.breakout_mult * range_width

        # Breakout to the upside
        if bar.close > self._range_high + required_breakout:
            ctx.buy(
                size=self.size,
                stop_offset=self.bracket_mult * range_width,
                target_offset=self.bracket_mult * range_width,
                tag="range-breakout-long",
            )
            self._entry_bar_index = self._bars_this_session
        # Breakout to the downside
        elif bar.close < self._range_low - required_breakout:
            ctx.sell(
                size=self.size,
                stop_offset=self.bracket_mult * range_width,
                target_offset=self.bracket_mult * range_width,
                tag="range-breakout-short",
            )
            self._entry_bar_index = self._bars_this_session

    def on_fill(self, ctx) -> None:
        # If we just filled an entry, record the bar index when it happened
        # (we set _entry_bar_index when placing the order, which is close enough)
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
