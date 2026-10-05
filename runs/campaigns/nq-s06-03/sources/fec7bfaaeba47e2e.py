"""Opening-range breakout with a trailing stop instead of a fixed target, and a
filter that requires the breakout to happen after a quiet opening.

The question: when price leaves the opening range, does the move hold or does the
range win? This strategy answers "the move holds" for breakouts that occur after
a quiet opening, where the opening range itself is narrow relative to the
previous session's range. The intuition is that a narrow opening range means the
opening auction did not resolve disagreement, so a breakout from it is more
likely to be the day's real direction rather than noise.

The opening range is the high and low of the first 30 minutes of the regular
session, measured on 5-minute bars. A breakout is a close beyond the range by at
least 0.25 * the previous session's daily range, plus one tick. The opening
range must be narrower than 0.5 * the previous session's range for the trade to
be taken. The stop is 1.5 * the band distance; there is no fixed target --
instead the position is closed when the bar closes back inside the opening range
or when the deadline of 10 bars expires.

Costs: the fill model is unfavorable and commission is 0.1 index points per
side, so the round-trip cost is at least 0.2 points plus the spread, which is at
least one tick (0.25 points) on NQ. The band and stop distances are stated in
index points and are deliberately wide enough to clear that cost.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_bars: int = 6,
        band_atr_mult: float = 0.25,
        confirmation_ticks: float = 0.25,
        stop_atr_mult: float = 1.5,
        max_hold_bars: int = 10,
        quiet_opening_mult: float = 0.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.band_atr_mult = band_atr_mult
        self.confirmation_ticks = confirmation_ticks
        self.stop_atr_mult = stop_atr_mult
        self.max_hold_bars = max_hold_bars
        self.quiet_opening_mult = quiet_opening_mult
        self.size = size

        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._bars_this_session = 0
        self._atr = None
        self._bars_held = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe("1d")

    def on_bars(self, ctx) -> None:
        if "1d" in ctx.bars:
            daily = ctx.history("1d")
            if len(daily) >= 2:
                prev = daily[-1]
                self._atr = prev.high - prev.low

        if self.interval not in ctx.bars:
            return
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._opening_high = None
            self._opening_low = None
            self._bars_held = 0

        self._bars_this_session += 1
        bar = ctx.bars[self.interval]

        if self._bars_this_session <= self.opening_bars:
            if self._opening_high is None or bar.high > self._opening_high:
                self._opening_high = bar.high
            if self._opening_low is None or bar.low < self._opening_low:
                self._opening_low = bar.low
            return

        if self._opening_high is None or self._opening_low is None or self._atr is None:
            return

        if ctx.position:
            self._bars_held += 1
            # Exit if the bar closes back inside the opening range or deadline.
            if self._bars_held >= self.max_hold_bars:
                ctx.close()
                self._bars_held = 0
            elif self._opening_low <= bar.close <= self._opening_high:
                ctx.close()
                self._bars_held = 0
            return

        opening_range = self._opening_high - self._opening_low
        if opening_range > self.quiet_opening_mult * self._atr:
            return

        band = self.band_atr_mult * self._atr
        upper = self._opening_high + band + self.confirmation_ticks
        lower = self._opening_low - band - self.confirmation_ticks

        if bar.close > upper:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_atr_mult * self._atr,
                tag="breakout-long",
            )
            self._bars_held = 0
        elif bar.close < lower:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_atr_mult * self._atr,
                tag="breakout-short",
            )
            self._bars_held = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_close":
            self._bars_held = 0
