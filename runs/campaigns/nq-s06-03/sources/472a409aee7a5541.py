"""Opening-range fade: fade breakouts that fail to hold beyond the opening range.

The question asks whether the move holds or the range wins. This strategy
answers "the range wins" by fading breakouts that occur after the opening
range has already seen a failed breakout. The opening range is the high and
low of the first 30 minutes of the regular session, measured on 5-minute bars.
A breakout is a close beyond the range by at least 0.25 * the previous
session's daily range, plus one tick. The first breakout in a session is
ignored; subsequent breakouts in the opposite direction are faded, on the
theory that the initial move exhausted the opening flow and the range will
reassert itself.

Entry is a market order against the breakout direction. The initial stop is
1.5 * the band distance from entry. The target is 1.0 * the band distance.
The deadline is 6 bars after entry, at which point any remaining position is
closed.

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
        band_atr_mult: float = 0.25,
        confirmation_ticks: float = 0.25,
        stop_atr_mult: float = 1.5,
        target_atr_mult: float = 1.0,
        max_hold_bars: int = 6,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.band_atr_mult = band_atr_mult
        self.confirmation_ticks = confirmation_ticks
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
        self._entry_price = None
        self._stop_price = None
        self._target_price = None
        self._position_side = None
        self._first_breakout_direction = None

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
            self._target_price = None
            self._position_side = None
            self._first_breakout_direction = None

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

        # Manage an existing position.
        if ctx.position:
            self._bars_held += 1
            if self._bars_held >= self.max_hold_bars:
                ctx.close()
                self._entry_price = None
                self._stop_price = None
                self._target_price = None
                self._position_side = None
                return

            # Let the bracket handle the exit; no manual trailing here.
            return

        # Flat: look for a breakout.
        band = self.band_atr_mult * self._atr
        upper = self._opening_high + band + self.confirmation_ticks
        lower = self._opening_low - band - self.confirmation_ticks

        breakout_dir = None
        if bar.close > upper:
            breakout_dir = "long"
        elif bar.close < lower:
            breakout_dir = "short"

        if breakout_dir is None:
            return

        # First breakout: record direction, do not trade.
        if self._first_breakout_direction is None:
            self._first_breakout_direction = breakout_dir
            return

        # Subsequent breakout in the opposite direction: fade it.
        if breakout_dir != self._first_breakout_direction:
            if breakout_dir == "long":
                stop_price = bar.close + self.stop_atr_mult * self._atr
                target_price = bar.close - self.target_atr_mult * self._atr
                ctx.sell(
                    size=self.size,
                    stop=stop_price,
                    target=target_price,
                    tag="fade-long",
                )
                self._entry_price = bar.close
                self._stop_price = stop_price
                self._target_price = target_price
                self._bars_held = 0
                self._position_side = "short"
            else:
                stop_price = bar.close - self.stop_atr_mult * self._atr
                target_price = bar.close + self.target_atr_mult * self._atr
                ctx.buy(
                    size=self.size,
                    stop=stop_price,
                    target=target_price,
                    tag="fade-short",
                )
                self._entry_price = bar.close
                self._stop_price = stop_price
                self._target_price = target_price
                self._bars_held = 0
                self._position_side = "long"

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_close":
            self._entry_price = None
            self._stop_price = None
            self._target_price = None
            self._position_side = None
            self._first_breakout_direction = None
