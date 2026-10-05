"""Opening-range breakout with a volatility-scaled band and a counter-trend fade,
using a tighter confirmation and a longer opening window.

The question asks whether the range wins or the move holds. Prior trials tested
both directions with a 30-minute opening window and a 0.25 * daily range band.
This strategy tests the "range wins" hypothesis with two changes that may reduce
noise: a 60-minute opening window (12 five-minute bars) and a smaller band
(0.15 * daily range). The longer window gives the opening range more time to
establish meaningful support/resistance; the smaller band makes the fade trigger
more often, which should increase exposure per session and reduce the chance of
missing the reversion.

The entry is always a fade: if the close is above the upper band, sell short;
if below the lower band, buy. The stop is 1.5 * the band distance; the target is
2.0 * the band distance. The deadline is 8 bars after entry, at which point any
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
        opening_bars: int = 12,         # 60 minutes of 5-minute bars
        band_atr_mult: float = 0.15,    # breakout distance as a fraction of daily range
        stop_atr_mult: float = 1.5,
        target_atr_mult: float = 2.0,
        max_hold_bars: int = 8,
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
            if self._bars_held >= self.max_hold_bars:
                ctx.close()
                self._bars_held = 0
            return

        band = self.band_atr_mult * self._atr
        upper = self._opening_high + band
        lower = self._opening_low - band

        if bar.close > upper:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_atr_mult * self._atr,
                target_offset=self.target_atr_mult * self._atr,
                tag="fade-long",
            )
            self._bars_held = 0
        elif bar.close < lower:
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
