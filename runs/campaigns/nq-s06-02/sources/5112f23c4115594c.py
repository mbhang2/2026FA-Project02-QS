"""Opening-range breakout with a symmetric bracket and an early-session filter.

The question: when price leaves the opening range, does the move hold or does
the range win?

This strategy answers "the move holds" for breakouts that occur within the first
90 minutes of the regular-hours session, with a stated condition that the
breakout must be at least 25% of the opening range width beyond the range.

Opening range: high and low of the first 30 minutes (6 bars on 5m).

Breakout definition: a 5-minute close strictly above the range high plus 0.25 *
range width, or strictly below the range low minus 0.25 * range width.

Entry: market order on the bar close that confirms the breakout.

Exit: a bracket with stop and target each 1.0x the range width, plus a hard
deadline of 90 minutes from entry. The deadline is checked on each subsequent
bar close; if reached, any working orders are cancelled and the position is
closed at market.

The strategy acts on every session where a qualifying breakout occurs within the
time window. The condition is stated in the code, not chosen after looking.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_bars: int = 6,           # 30 minutes of 5m bars
        breakout_mult: float = 0.25,   # must exceed range by 25% of range width
        bracket_mult: float = 1.0,     # stop/target distance as multiple of range
        max_hold_minutes: int = 90,    # hard deadline after entry
        entry_window_minutes: int = 90,  # only take breakouts within this window
        size: int = 1,
    ):
        self.interval = interval
        self.range_bars = range_bars
        self.breakout_mult = breakout_mult
        self.bracket_mult = bracket_mult
        self.max_hold_minutes = max_hold_minutes
        self.entry_window_minutes = entry_window_minutes
        self.size = size

        # Per-session state
        self._session = None
        self._bars_this_session = 0
        self._range_high = None
        self._range_low = None
        self._entry_ts = None  # timestamp when the entry order was placed

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Track session and bar count
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._range_high = None
            self._range_low = None
            self._entry_ts = None

        # Only act on our interval
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
            if self._entry_ts is not None:
                elapsed_minutes = (ctx.ts - self._entry_ts) / 60_000_000_000
                if elapsed_minutes >= self.max_hold_minutes:
                    # Cancel any working bracket orders and close
                    ctx.cancel_all()
                    ctx.close()
                    self._entry_ts = None
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

        # Check time window: only enter within entry_window_minutes of session open
        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.entry_window_minutes:
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
            self._entry_ts = ctx.ts
        # Breakout to the downside
        elif bar.close < self._range_low - required_breakout:
            ctx.sell(
                size=self.size,
                stop_offset=self.bracket_mult * range_width,
                target_offset=self.bracket_mult * range_width,
                tag="range-breakout-short",
            )
            self._entry_ts = ctx.ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
