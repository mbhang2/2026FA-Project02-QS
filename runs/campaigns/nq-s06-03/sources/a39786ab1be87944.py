"""Opening-range breakout with a volatility-scaled band and a stated deadline.

This strategy answers "the move holds" by entering breakouts of the opening
range, but it scales the breakout distance by recent realized volatility and
requires a close beyond the band. It exits on a fixed deadline, so the holding
period is honest.

The opening range is the high and low of the first 30 minutes of the regular
session, measured on 5-minute bars. A breakout is a close beyond the range by at
least 0.25 * the previous session's range, which adapts the band to recent
volatility. The entry is in the direction of the breakout; the stop is 1.5 * the
band distance from entry; the target is 2.0 * the band distance. The deadline is
10 bars after entry, at which point any remaining position is closed.

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
        opening_bars: int = 6,          # 30 minutes of 5-minute bars
        band_atr_mult: float = 0.25,    # breakout distance as a fraction of ATR
        stop_atr_mult: float = 1.5,     # stop distance as a fraction of ATR
        target_atr_mult: float = 2.0,   # target distance as a fraction of ATR
        max_hold_bars: int = 10,        # deadline, in bars
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.band_atr_mult = band_atr_mult
        self.stop_atr_mult = stop_atr_mult
        self.target_atr_mult = target_atr_mult
        self.max_hold_bars = max_hold_bars
        self.size = size

        # State per session
        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._bars_this_session = 0
        self._atr = None

        # State per position
        self._bars_held = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        # The 1d subscription gives us the previous session's daily range.
        ctx.subscribe("1d")

    def on_bars(self, ctx) -> None:
        # Update the ATR when a daily bar closes (session boundary).
        if "1d" in ctx.bars:
            daily = ctx.history("1d")
            if len(daily) >= 2:
                prev = daily[-1]
                self._atr = prev.high - prev.low

        # Only act on the 5-minute bars.
        if self.interval not in ctx.bars:
            return
        if not ctx.actionable:
            return

        # Session bookkeeping
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._opening_high = None
            self._opening_low = None
            self._bars_held = 0

        self._bars_this_session += 1
        bar = ctx.bars[self.interval]

        # During the opening period, track the range.
        if self._bars_this_session <= self.opening_bars:
            if self._opening_high is None or bar.high > self._opening_high:
                self._opening_high = bar.high
            if self._opening_low is None or bar.low < self._opening_low:
                self._opening_low = bar.low
            return

        # We need an opening range and an ATR to act.
        if self._opening_high is None or self._opening_low is None or self._atr is None:
            return

        # Manage an existing position.
        if ctx.position:
            self._bars_held += 1
            if self._bars_held >= self.max_hold_bars:
                ctx.close()
                self._bars_held = 0
            return

        # Flat: look for a breakout.
        band = self.band_atr_mult * self._atr
        if bar.close > self._opening_high + band:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_atr_mult * self._atr,
                target_offset=self.target_atr_mult * self._atr,
                tag="breakout-long",
            )
            self._bars_held = 0
        elif bar.close < self._opening_low - band:
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
