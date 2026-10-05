"""Opening-range breakout that enters only after a confirmed close beyond the
band and holds until the move stalls, with a trailing stop and a hard deadline.

The question asks whether the move holds or the range wins. This strategy
answers "the move holds" but with a more selective entry and a trailing stop
that follows the price closely once the trade is in profit. The opening range
is the high and low of the first 30 minutes of the regular session, measured
on 5-minute bars. A breakout is a close beyond the range by at least 0.4 *
the previous session's daily range, plus one tick. The initial stop is 1.5 *
the band distance. Once the position is in profit by at least 1.0 * the band
distance, the stop is trailed to the bar's low (for longs) or high (for
shorts), never moved against the position. The deadline is 8 bars after entry,
at which point any remaining position is closed.

Costs: the fill model is unfavorable and commission is 0.1 index points per
side, so the round-trip cost is at least 0.2 points plus the spread, which is
at least one tick (0.25 points) on NQ. The band and stop distances are stated
in index points and are deliberately wide enough to clear that cost.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_bars: int = 6,
        band_atr_mult: float = 0.4,
        confirmation_ticks: float = 0.25,
        stop_atr_mult: float = 1.5,
        trail_trigger_atr_mult: float = 1.0,
        max_hold_bars: int = 8,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.band_atr_mult = band_atr_mult
        self.confirmation_ticks = confirmation_ticks
        self.stop_atr_mult = stop_atr_mult
        self.trail_trigger_atr_mult = trail_trigger_atr_mult
        self.max_hold_bars = max_hold_bars
        self.size = size

        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._bars_this_session = 0
        self._atr = None
        self._bars_held = 0
        self._entry_price = None
        self._stop_price = None
        self._position_side = None

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
            self._entry_price = None
            self._stop_price = None
            self._position_side = None

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
                self._entry_price = None
                self._stop_price = None
                self._position_side = None
                return

            # Trail the stop when in profit by at least the trigger distance.
            if self._entry_price is not None and self._stop_price is not None:
                if self._position_side == "long":
                    profit = bar.close - self._entry_price
                    if profit >= self.trail_trigger_atr_mult * self._atr:
                        new_stop = bar.low
                        if new_stop > self._stop_price:
                            self._stop_price = new_stop
                            stops = ctx.find_orders(tag="protective-stop")
                            if stops:
                                ctx.modify(stops[0].id, trigger=self._stop_price)
                elif self._position_side == "short":
                    profit = self._entry_price - bar.close
                    if profit >= self.trail_trigger_atr_mult * self._atr:
                        new_stop = bar.high
                        if new_stop < self._stop_price:
                            self._stop_price = new_stop
                            stops = ctx.find_orders(tag="protective-stop")
                            if stops:
                                ctx.modify(stops[0].id, trigger=self._stop_price)
            return

        band = self.band_atr_mult * self._atr
        upper = self._opening_high + band + self.confirmation_ticks
        lower = self._opening_low - band - self.confirmation_ticks

        if bar.close > upper:
            stop_price = bar.close - self.stop_atr_mult * self._atr
            ctx.buy(
                size=self.size,
                stop=stop_price,
                tag="breakout-long",
            )
            ctx.sell(
                size=self.size,
                type="stop",
                trigger=stop_price,
                reduce_only=True,
                tag="protective-stop",
            )
            self._entry_price = bar.close
            self._stop_price = stop_price
            self._bars_held = 0
            self._position_side = "long"
        elif bar.close < lower:
            stop_price = bar.close + self.stop_atr_mult * self._atr
            ctx.sell(
                size=self.size,
                stop=stop_price,
                tag="breakout-short",
            )
            ctx.buy(
                size=self.size,
                type="stop",
                trigger=stop_price,
                reduce_only=True,
                tag="protective-stop",
            )
            self._entry_price = bar.close
            self._stop_price = stop_price
            self._bars_held = 0
            self._position_side = "short"

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_close":
            self._entry_price = None
            self._stop_price = None
            self._position_side = None
