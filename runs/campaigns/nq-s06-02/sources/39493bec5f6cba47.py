"""Opening-range breakout: the move holds, with volatility-scaled confirmation.

The question: when price leaves the opening range, does the move hold or does
the range win? This strategy answers "the move holds" for breakouts that are
large relative to recent realized volatility.

Opening range: high and low of the first 30 minutes of the regular-hours session.

Breakout definition: a 5-minute bar close beyond the opening range by at least
0.5 * the average true range of the prior 5 sessions' opening ranges. This
conditions the "leaving" decision on how much the market has been moving
recently, as the campaign asks.

Entry: on the bar close that confirms the breakout, in the breakout direction.

Exit: a stated deadline of 60 minutes from entry, at which point the position is
closed at market. A protective stop is placed at the opposite side of the
opening range.

The rule acts on every session where a qualifying breakout occurs.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_minutes: int = 30,
        breakout_atr_mult: float = 0.5,
        size: int = 1,
        hold_minutes: int = 60,
        atr_lookback_sessions: int = 5,
    ):
        self.interval = interval
        self.range_minutes = range_minutes
        self.breakout_atr_mult = breakout_atr_mult
        self.size = size
        self.hold_minutes = hold_minutes
        self.atr_lookback_sessions = atr_lookback_sessions

        self._session = None
        self._session_open_ts = None
        self._range_high = None
        self._range_low = None
        self._entry_ts = None
        self._prior_range_widths: list[float] = []

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return

        # Session boundary: reset and store the prior session's range width
        if ctx.session != self._session:
            if self._session is not None and self._range_high is not None:
                self._prior_range_widths.append(self._range_high - self._range_low)
                if len(self._prior_range_widths) > self.atr_lookback_sessions:
                    self._prior_range_widths.pop(0)
            self._session = ctx.session
            self._session_open_ts = ctx.session_start
            self._range_high = None
            self._range_low = None
            self._entry_ts = None

        # Manage existing position: close at deadline
        if ctx.position:
            if self._entry_ts is not None:
                elapsed = (ctx.ts - self._entry_ts) / 1_000_000_000
                if elapsed >= self.hold_minutes * 60:
                    ctx.close()
                    self._entry_ts = None
            return

        # No position: collect opening range and look for breakout
        history = ctx.history(self.interval)
        if not history:
            return

        # Build opening range from bars that start within the first range_minutes
        if self._range_high is None:
            range_end_ts = self._session_open_ts + self.range_minutes * 60 * 1_000_000_000
            range_bars = [b for b in history if b.open_ts < range_end_ts]
            if range_bars:
                self._range_high = max(b.high for b in range_bars)
                self._range_low = min(b.low for b in range_bars)

        if self._range_high is None or self._range_low is None:
            return

        range_width = self._range_high - self._range_low
        if range_width <= 0:
            return

        # Compute breakout threshold from recent range widths
        if len(self._prior_range_widths) >= self.atr_lookback_sessions:
            avg_range = sum(self._prior_range_widths) / len(self._prior_range_widths)
            threshold = self.breakout_atr_mult * avg_range
        else:
            # Not enough history: fall back to a fraction of the current range
            threshold = self.breakout_atr_mult * range_width

        last = history[-1]
        breakout_up = last.close > self._range_high + threshold
        breakout_down = last.close < self._range_low - threshold

        if not (breakout_up or breakout_down):
            return

        # Enter in the breakout direction, with a protective stop at the
        # opposite side of the opening range.
        if breakout_up:
            stop_price = self._range_low
            ctx.buy(
                size=self.size,
                stop=stop_price,
                tag="range-breakout-long",
            )
        else:
            stop_price = self._range_high
            ctx.sell(
                size=self.size,
                stop=stop_price,
                tag="range-breakout-short",
            )

        self._entry_ts = ctx.ts

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
