"""Opening-range breakout with a volatility-scaled band, but the entry is
conditional on the breakout being followed by a pullback to the range edge.

The question: when price leaves the opening range, does the move hold or does
the range win? Prior trials answered "the move holds" by entering breakouts
immediately and produced a small, positive but economically marginal score.
This strategy answers "the range wins" for the first move, but then "the move
holds" for the second, more deliberate move: after a breakout that fails and
price returns inside the range, the next breakout is more likely to be real.
This is a classic "opening range reversal then continuation" pattern.

Mechanics:
- Opening range is the high/low of the first 30 minutes (6 x 5m bars).
- A "breakout" is a close beyond the range by at least 0.25 * ATR, where ATR
  is the previous session's daily range.
- After a breakout, we wait for price to close back inside the range (the
  "reversal"). This confirms the range won the first battle.
- Once price has returned inside, the next close beyond the range in either
  direction is considered a genuine breakout and we enter in that direction.
- The stop is 1.5 * ATR, target is 2.0 * ATR, and a hard deadline of 10 bars
  closes any remaining position.

This is distinct from prior trials because it requires a failed breakout
first, which filters out the initial noise and should reduce the number of
marginal trades while keeping the ones with a confirmed range rejection.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_bars: int = 6,
        atr_lookback: int = 10,
        band_atr_mult: float = 0.25,
        stop_atr_mult: float = 1.5,
        target_atr_mult: float = 2.0,
        max_hold_bars: int = 10,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.atr_lookback = atr_lookback
        self.band_atr_mult = band_atr_mult
        self.stop_atr_mult = stop_atr_mult
        self.target_atr_mult = target_atr_mult
        self.max_hold_bars = max_hold_bars
        self.size = size

        # Session state
        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._bars_this_session = 0
        self._atr = None

        # Pattern state
        self._breakout_occurred = False
        self._returned_inside = False

        # Position state
        self._bars_held = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe("1d")

    def on_bars(self, ctx) -> None:
        # Update ATR from daily bars
        if "1d" in ctx.bars:
            daily = ctx.history("1d")
            if len(daily) >= 2:
                self._atr = daily[-1].high - daily[-1].low

        if self.interval not in ctx.bars:
            return
        if not ctx.actionable:
            return

        # Session reset
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._opening_high = None
            self._opening_low = None
            self._breakout_occurred = False
            self._returned_inside = False
            self._bars_held = 0

        self._bars_this_session += 1
        bar = ctx.bars[self.interval]

        # Track opening range
        if self._bars_this_session <= self.opening_bars:
            if self._opening_high is None or bar.high > self._opening_high:
                self._opening_high = bar.high
            if self._opening_low is None or bar.low < self._opening_low:
                self._opening_low = bar.low
            return

        if self._opening_high is None or self._opening_low is None or self._atr is None:
            return

        # Manage existing position
        if ctx.position:
            self._bars_held += 1
            if self._bars_held >= self.max_hold_bars:
                ctx.close()
                self._bars_held = 0
            return

        band = self.band_atr_mult * self._atr
        long_trigger = self._opening_high + band
        short_trigger = self._opening_low - band

        # Track the pattern
        if not self._breakout_occurred:
            if bar.close > long_trigger or bar.close < short_trigger:
                self._breakout_occurred = True
                self._returned_inside = False
        elif not self._returned_inside:
            # After a breakout, look for a return inside the range
            if self._opening_low <= bar.close <= self._opening_high:
                self._returned_inside = True

        # Only trade after a failed breakout and return inside
        if self._breakout_occurred and self._returned_inside:
            if bar.close > long_trigger:
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_atr_mult * self._atr,
                    target_offset=self.target_atr_mult * self._atr,
                    tag="breakout-long-after-reversal",
                )
                self._bars_held = 0
                self._breakout_occurred = False
                self._returned_inside = False
            elif bar.close < short_trigger:
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_atr_mult * self._atr,
                    target_offset=self.target_atr_mult * self._atr,
                    tag="breakout-short-after-reversal",
                )
                self._bars_held = 0
                self._breakout_occurred = False
                self._returned_inside = False

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        if ctx.event.kind == "session_close":
            self._breakout_occurred = False
            self._returned_inside = False
            self._bars_held = 0
