"""Opening-range breakout that conditions on the distance to the nearest swing
level, with a volatility-scaled band and a stated deadline.

The question asks whether the move holds or the range wins. This strategy
answers "the move holds" but only when there is no nearby swing level ahead of
the entry. The intuition, drawn from the open question in the campaign brief,
is that a nearby swing level marks a stretch of small movement and may suppress
the follow-through that a breakout needs. By requiring the distance to the
nearest confirmed swing level to be at least a stated multiple of the band, we
trade only breakouts with room to run.

The opening range is the high and low of the first 30 minutes of the regular
session, measured on 5-minute bars. A breakout is a close beyond the range by
at least 0.25 * the previous session's daily range, plus one tick. The nearest
swing level is the highest high or lowest low of the prior 20 bars, excluding
the opening range itself. If that level is within 1.5 * the band distance of
the opening range boundary, the trade is skipped. The stop is 1.5 * the band
distance; the target is 2.0 * the band distance. The deadline is 10 bars after
entry, at which point any remaining position is closed.

Costs: the fill model is unfavorable and commission is 0.1 index points per
side, so the round-trip cost is at least 0.2 points plus the spread, which is
at least one tick (0.25 points) on NQ. The band and stop/target distances are
stated in index points and are deliberately wide enough to clear that cost.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_bars: int = 6,
        swing_lookback: int = 20,
        band_atr_mult: float = 0.25,
        confirmation_ticks: float = 0.25,
        stop_atr_mult: float = 1.5,
        target_atr_mult: float = 2.0,
        swing_distance_mult: float = 1.5,
        max_hold_bars: int = 10,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.swing_lookback = swing_lookback
        self.band_atr_mult = band_atr_mult
        self.confirmation_ticks = confirmation_ticks
        self.stop_atr_mult = stop_atr_mult
        self.target_atr_mult = target_atr_mult
        self.swing_distance_mult = swing_distance_mult
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
        upper = self._opening_high + band + self.confirmation_ticks
        lower = self._opening_low - band - self.confirmation_ticks

        # Compute the nearest swing level from history, excluding the opening
        # range itself.
        history = ctx.history(self.interval)
        if len(history) < self.opening_bars + self.swing_lookback:
            return

        # The bars after the opening range, up to the current bar.
        post_open = history[-self.swing_lookback:]
        if len(post_open) < self.swing_lookback:
            return

        swing_high = max(b.high for b in post_open)
        swing_low = min(b.low for b in post_open)

        min_distance = self.swing_distance_mult * band

        if bar.close > upper:
            # Distance from the opening high to the nearest swing high above it.
            # If there is a swing high within min_distance of the opening high,
            # skip the trade.
            if swing_high - self._opening_high < min_distance:
                return
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_atr_mult * self._atr,
                target_offset=self.target_atr_mult * self._atr,
                tag="breakout-long",
            )
            self._bars_held = 0
        elif bar.close < lower:
            # Distance from the opening low to the nearest swing low below it.
            if self._opening_low - swing_low < min_distance:
                return
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_atr_mult * self._atr,
                target_offset=self.target_atr_mult * self._atr,
                tag="breakout-short",
            )
            self._bars_held = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_close":
            self._bars_held = 0
