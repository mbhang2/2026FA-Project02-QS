"""Opening-range breakout with volatility-scaled band, directional filter, and
mean-reversion fade after a failed breakout.

The question: when price leaves the opening range, does the move hold or does the
range win? This strategy answers "the range wins" by fading breakouts that fail
to hold. It enters in the opposite direction of a breakout when price closes back
inside the opening range after exceeding it, indicating the breakout was false.
The entry is conditional on the previous session's direction to avoid fading
strong trends, and the position is managed with a fixed stop, target, and
deadline.

The opening range is the high and low of the first 30 minutes of the regular
session, measured on 5-minute bars. A breakout is a close beyond the range by at
least 0.25 * the previous session's daily range. A fade is triggered when a bar
closes back inside the range after a breakout, in the opposite direction of the
breakout. The stop is 1.5 * the band distance from entry; the target is 2.0 *
the band distance. The deadline is 10 bars after entry, at which point any
remaining position is closed.

Costs: the fill model is unfavorable and commission is 0.1 index points per side,
so the round-trip cost is at least 0.2 points plus the spread, which is at least
one tick (0.25 points) on NQ. The band and stop/target distances are stated in
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
        stop_atr_mult: float = 1.5,
        target_atr_mult: float = 2.0,
        max_hold_bars: int = 10,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.band_atr_mult = band_atr_mult
        self.stop_atr_mult = stop_atr_mult
        self.target_atr_mult = target_atr_mult
        self.max_hold_bars = max_hold_bars
        self.size = size

        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._bars_this_session = 0
        self._atr = None
        self._prior_direction = 0  # +1 if prior session up, -1 if down
        self._bars_held = 0
        self._breakout_direction = 0  # +1 for long breakout, -1 for short
        self._breakout_active = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe("1d")

    def on_bars(self, ctx) -> None:
        if "1d" in ctx.bars:
            daily = ctx.history("1d")
            if len(daily) >= 2:
                prev = daily[-1]
                self._atr = prev.high - prev.low
                self._prior_direction = 1 if prev.close > prev.open else -1

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
            self._breakout_direction = 0
            self._breakout_active = False

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
            if self._bars_held >= self.max_hold_bars:
                ctx.close()
                self._bars_held = 0
            return

        band = self.band_atr_mult * self._atr
        long_trigger = self._opening_high + band
        short_trigger = self._opening_low - band

        # Track breakout state
        if not self._breakout_active:
            if bar.close > long_trigger:
                self._breakout_direction = 1
                self._breakout_active = True
            elif bar.close < short_trigger:
                self._breakout_direction = -1
                self._breakout_active = True
            return

        # If a breakout was active, check for a fade back inside the range
        if self._breakout_direction == 1 and bar.close < self._opening_high:
            # Long breakout failed, fade short if prior session was down
            if self._prior_direction < 0:
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_atr_mult * self._atr,
                    target_offset=self.target_atr_mult * self._atr,
                    tag="fade-short",
                )
                self._bars_held = 0
            self._breakout_active = False
            self._breakout_direction = 0
        elif self._breakout_direction == -1 and bar.close > self._opening_low:
            # Short breakout failed, fade long if prior session was up
            if self._prior_direction > 0:
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_atr_mult * self._atr,
                    target_offset=self.target_atr_mult * self._atr,
                    tag="fade-long",
                )
                self._bars_held = 0
            self._breakout_active = False
            self._breakout_direction = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_close":
            self._bars_held = 0
            self._breakout_active = False
            self._breakout_direction = 0
