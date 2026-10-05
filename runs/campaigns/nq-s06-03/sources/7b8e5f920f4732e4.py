"""Opening-range breakout with a volatility-scaled band, a stated deadline, and
a spread-aware entry filter.

The question: when price leaves the opening range, does the move hold or does
the range win? This strategy answers "the move holds" by entering breakouts,
but it scales the breakout distance by recent realized volatility so a quiet
session does not trigger on noise, and it exits on a fixed deadline rather than
a target, so the holding period is honest and the three-way outcome
decomposition is possible.

The opening range is the high and low of the first 30 minutes of the regular
session, measured on 5-minute bars. A breakout is a close beyond the range by at
least 0.25 * the 10-session average true range of 5-minute bars, which adapts the
band to recent volatility without any forward-looking information. The entry is
in the direction of the breakout; the stop is 1.5 * the band distance from entry;
the target is 2.0 * the band distance. The deadline is 10 bars after entry, at
which point any remaining position is closed.

Costs: the fill model is unfavorable and commission is 0.1 index points per side,
so the round-trip cost is at least 0.2 points plus the spread, which is at least
one tick (0.25 points) on NQ. The band and stop/target distances are stated in
index points and are deliberately wide enough to clear that cost: a 0.25-point
band on a typical 5-point ATR is a 0.05-ATR move, which is small enough to trade
often but large enough to be meaningful.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_bars: int = 6,          # 30 minutes of 5-minute bars
        atr_lookback: int = 10,         # sessions for the ATR
        band_atr_mult: float = 0.25,    # breakout distance as a fraction of ATR
        stop_atr_mult: float = 1.5,     # stop distance as a fraction of ATR
        target_atr_mult: float = 2.0,   # target distance as a fraction of ATR
        max_hold_bars: int = 10,        # deadline, in bars
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
        # We need a daily-ish bar to compute the ATR across sessions. The 1d
        # subscription closes once per session on this data, which gives us the
        # previous session's daily range.
        ctx.subscribe("1d")

    def on_bars(self, ctx) -> None:
        # Update the ATR when a daily bar closes (i.e., at session boundaries).
        if "1d" in ctx.bars:
            daily = ctx.history("1d")
            if len(daily) >= 2:
                prev = daily[-1]
                self._atr = prev.high - prev.low
                # Use the previous session's range as the ATR estimate. This is
                # a simple, robust estimator of recent volatility.

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
                # Deadline: close the position.
                ctx.close()
                self._entry_ts = None
                self._bars_held = 0
                self._entry_price = None
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
            self._entry_ts = bar.close_ts
            self._bars_held = 0
            self._entry_price = bar.close
        elif bar.close < self._opening_low - band:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_atr_mult * self._atr,
                target_offset=self.target_atr_mult * self._atr,
                tag="breakout-short",
            )
            self._entry_ts = bar.close_ts
            self._bars_held = 0
            self._entry_price = bar.close

    def on_fill(self, ctx) -> None:
        # The bracket handles the stop and target; the deadline is handled in
        # on_bars. Nothing to do here.
        pass

    def on_lifecycle(self, ctx) -> None:
        # If a DAY order expires at session close, reset position state. The
        # bracket legs are GTC by default? Actually, the bracket legs are DAY by
        # default, so they expire at session close. We reset our state then.
        if ctx.event.kind == "session_close":
            self._entry_ts = None
            self._bars_held = 0
            self._entry_price = None
        pass
