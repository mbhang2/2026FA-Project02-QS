"""Opening-range breakout with an adaptive volatility band and a fade variant.

This strategy tests the "range wins" hypothesis with a twist: instead of fading
every breakout, it fades only breakouts that occur after the opening range has
already been violated once in the same direction. The idea is that the first
breakout may be genuine, but a second breakout in the same direction is more
likely to be a false move that reverts.

The opening range is the high and low of the first 30 minutes (6 bars of 5m).
A breakout is a close beyond the range by at least 0.25 * the previous session's
daily range. We track how many times each side has been broken. If a side has
already been broken once, we fade the second breakout. Otherwise, we take the
first breakout in the direction of the move (trend-following).

This conditional approach aims to capture both regimes: early breakouts tend to
hold, while repeated breakouts are more likely to revert.

Costs: unfavorable fills and 0.1 points commission per side. Stops and targets
are scaled by the previous session's range.
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
        self._bars_held = 0
        self._long_breakouts = 0
        self._short_breakouts = 0

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
            self._long_breakouts = 0
            self._short_breakouts = 0

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
        upper = self._opening_high + band
        lower = self._opening_low - band

        if bar.close > upper:
            self._long_breakouts += 1
            if self._long_breakouts == 1:
                # First breakout: take the trend.
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_atr_mult * self._atr,
                    target_offset=self.target_atr_mult * self._atr,
                    tag="first-long",
                )
            else:
                # Second or later breakout: fade it.
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_atr_mult * self._atr,
                    target_offset=self.target_atr_mult * self._atr,
                    tag="fade-long",
                )
            self._bars_held = 0
        elif bar.close < lower:
            self._short_breakouts += 1
            if self._short_breakouts == 1:
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_atr_mult * self._atr,
                    target_offset=self.target_atr_mult * self._atr,
                    tag="first-short",
                )
            else:
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_atr_mult * self._atr,
                    target_offset=self.target_atr_mult * self._atr,
                    tag="fade-short",
                )
            self._bars_held = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_close":
            self._bars_held = 0
