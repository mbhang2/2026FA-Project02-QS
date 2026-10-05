"""Opening-range breakout with a tighter, more selective entry and a trailing stop.

The question asks whether the move holds or the range wins. Prior trials have
tested both directions with mixed results. This strategy refines the "move
holds" thesis by requiring a stronger breakout — a close beyond the opening
range by at least 0.5 * the previous session's daily range — and by using a
wider initial stop and a trailing stop to let winners run while cutting losers
quickly. The deadline remains 10 bars.

The opening range is the high and low of the first 30 minutes of the regular
session, measured on 5-minute bars. A breakout is a close beyond the range by
at least 0.5 * the previous session's daily range. The entry is in the
direction of the breakout. The initial stop is 2.0 * the band distance; there
is no fixed target. Instead, the stop trails the price by 1.5 * the band
distance once the position is in profit. The deadline is 10 bars after entry,
at which point any remaining position is closed.

Costs: the fill model is unfavorable and commission is 0.1 index points per
side, so the round-trip cost is at least 0.2 points plus the spread, which is
at least one tick (0.25 points) on NQ. The band distance is deliberately wide
enough to clear that cost.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_bars: int = 6,
        band_atr_mult: float = 0.5,
        initial_stop_atr_mult: float = 2.0,
        trail_atr_mult: float = 1.5,
        max_hold_bars: int = 10,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.band_atr_mult = band_atr_mult
        self.initial_stop_atr_mult = initial_stop_atr_mult
        self.trail_atr_mult = trail_atr_mult
        self.max_hold_bars = max_hold_bars
        self.size = size

        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._bars_this_session = 0
        self._atr: float | None = None
        self._bars_held = 0
        self._stop_order_id = None
        self._entry_price: float | None = None

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
            self._stop_order_id = None
            self._entry_price = None

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
                self._stop_order_id = None
                self._entry_price = None
                return

            # Trail the stop if we have a stop order and a profit.
            if self._stop_order_id is not None and self._entry_price is not None:
                stops = ctx.find_orders(tag="trailing-stop")
                if stops:
                    if ctx.position > 0:
                        new_stop = ctx.last_price - self.trail_atr_mult * self._atr
                        if new_stop > stops[0].stop_price:
                            ctx.modify(stops[0].id, trigger=new_stop)
                    elif ctx.position < 0:
                        new_stop = ctx.last_price + self.trail_atr_mult * self._atr
                        if new_stop < stops[0].stop_price:
                            ctx.modify(stops[0].id, trigger=new_stop)
            return

        band = self.band_atr_mult * self._atr
        upper = self._opening_high + band
        lower = self._opening_low - band

        if bar.close > upper:
            ctx.buy(size=self.size, tag="breakout-long")
            self._entry_price = bar.close
            self._bars_held = 0
            self._place_stop(ctx, is_long=True)
        elif bar.close < lower:
            ctx.sell(size=self.size, tag="breakout-short")
            self._entry_price = bar.close
            self._bars_held = 0
            self._place_stop(ctx, is_long=False)

    def _place_stop(self, ctx, is_long: bool) -> None:
        if self._atr is None or self._entry_price is None:
            return
        stop_distance = self.initial_stop_atr_mult * self._atr
        if is_long:
            trigger = self._entry_price - stop_distance
            ctx.sell(
                size=self.size,
                type="stop",
                trigger=trigger,
                reduce_only=True,
                tag="trailing-stop",
            )
        else:
            trigger = self._entry_price + stop_distance
            ctx.buy(
                size=self.size,
                type="stop",
                trigger=trigger,
                reduce_only=True,
                tag="trailing-stop",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_close":
            self._bars_held = 0
            self._stop_order_id = None
            self._entry_price = None
