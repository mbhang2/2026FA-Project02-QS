"""Opening-range fade with a volatility-scaled band and a stated deadline.

The question: when price leaves the opening range, does the move hold or does the
range win? The previous breakout attempts scored near zero, suggesting the move
does not hold. This strategy answers "the range wins" by fading breakouts: when
price closes beyond the opening range by a volatility-scaled distance, enter in
the opposite direction, expecting a reversion back inside the range.

The opening range is the high and low of the first 30 minutes of the regular
session, measured on 5-minute bars. A fade signal occurs when the close exceeds
the range by at least 0.25 * the previous session's daily range. The entry is
against the breakout; the stop is 1.5 * the band distance; the target is 2.0 *
the band distance. The deadline is 10 bars after entry, at which point any
remaining position is closed.

Costs: the fill model is unfavorable and commission is 0.1 index points per side.
The band and stop/target distances are stated in index points and are wide enough
to clear the round-trip cost on a typical session.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_bars: int = 6,          # 30 minutes of 5-minute bars
        band_atr_mult: float = 0.25,    # fade distance as a fraction of daily range
        stop_atr_mult: float = 1.5,     # stop distance as a fraction of daily range
        target_atr_mult: float = 2.0,   # target distance as a fraction of daily range
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
        self._entry_ts = None
        self._bars_held = 0
        self._entry_price = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe("1d")

    def on_bars(self, ctx) -> None:
        # Update the ATR when a daily bar closes (i.e., at session boundaries).
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
            self._entry_ts = None
            self._bars_held = 0
            self._entry_price = None

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
                self._entry_ts = None
                self._bars_held = 0
                self._entry_price = None
            return

        # Flat: look for a fade signal.
        band = self.band_atr_mult * self._atr
        if bar.close > self._opening_high + band:
            # Price broke out above the range; fade it (sell).
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_atr_mult * self._atr,
                target_offset=self.target_atr_mult * self._atr,
                tag="fade-short",
            )
            self._entry_ts = bar.close_ts
            self._bars_held = 0
            self._entry_price = bar.close
        elif bar.close < self._opening_low - band:
            # Price broke out below the range; fade it (buy).
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_atr_mult * self._atr,
                target_offset=self.target_atr_mult * self._atr,
                tag="fade-long",
            )
            self._entry_ts = bar.close_ts
            self._bars_held = 0
            self._entry_price = bar.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_close":
            self._entry_ts = None
            self._bars_held = 0
            self._entry_price = None
