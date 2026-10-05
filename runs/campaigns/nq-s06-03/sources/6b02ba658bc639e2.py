"""Opening-range fade: sell strength above the range, buy weakness below it.

The question asks whether the move holds or the range wins. This strategy
answers "the range wins" by fading breakouts, but only when the move is
large relative to recent volatility and occurs early in the session, when
the opening flow is most likely to exhaust itself. It uses a volatility-
scaled band, requires confirmation beyond the band, and exits on a fixed
deadline to keep the holding period honest.

The opening range is the high and low of the first 30 minutes (6 bars of
5m). A fade signal is a close beyond the range by at least 0.35 * the
previous session's daily range, plus one tick. Entry is against the move;
stop is 1.5 * the band distance; target is 2.0 * the band distance. The
deadline is 8 bars after entry. The strategy only trades during the first
three hours after the opening range completes, when mean reversion is
expected to be strongest.

Costs: commission is 0.1 index points per side and the fill model is
unfavorable, so the round-trip cost is at least 0.2 points plus the
spread. The band is deliberately wide enough to clear that cost while
still capturing the exhaustion move.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_bars: int = 6,
        band_mult: float = 0.35,
        confirmation_ticks: float = 0.25,
        stop_mult: float = 1.5,
        target_mult: float = 2.0,
        max_hold_bars: int = 8,
        max_session_bars: int = 36,  # 3 hours after the opening range
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.band_mult = band_mult
        self.confirmation_ticks = confirmation_ticks
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.max_hold_bars = max_hold_bars
        self.max_session_bars = max_session_bars
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

        if self._bars_this_session > self.max_session_bars:
            return

        band = self.band_mult * self._atr
        long_trigger = self._opening_high + band + self.confirmation_ticks
        short_trigger = self._opening_low - band - self.confirmation_ticks

        if bar.close > long_trigger:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_mult * self._atr,
                target_offset=self.target_mult * self._atr,
                tag="fade-short",
            )
            self._bars_held = 0
        elif bar.close < short_trigger:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_mult * self._atr,
                target_offset=self.target_mult * self._atr,
                tag="fade-long",
            )
            self._bars_held = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_close":
            self._bars_held = 0
