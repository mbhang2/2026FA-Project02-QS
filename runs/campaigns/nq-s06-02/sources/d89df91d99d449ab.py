"""Opening-range breakout with volatility-scaled confirmation, early-session focus, and a strict deadline.

The question: when price leaves the opening range, does the move hold or does
the range win?

This strategy answers "the move holds" for breakouts that occur within the first
90 minutes of the regular-hours session, when the breakout is large relative to
recent realized volatility.

Opening range: high and low of the first 30 minutes (6 bars on 5m).

Breakout definition: a 5-minute close strictly above the range high plus
0.5 * average true range of the prior 5 sessions' opening ranges, or strictly
below the range low minus that distance. This conditions the "leaving" decision
on how much the market has been moving recently, as the campaign asks.

Entry: market order on the bar close that confirms the breakout, in the
breakout direction (the move holds).

Exit: a stated deadline of 60 minutes from entry, at which point the position is
closed at market. A protective stop is placed at the opposite side of the
opening range via bracket.

The strategy acts on every session where a qualifying breakout occurs within the
time window. The condition is stated in the code, not chosen after looking.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_bars: int = 6,           # 30 minutes of 5m bars
        breakout_atr_mult: float = 0.5,  # breakout must exceed range by this multiple of recent ATR
        atr_lookback_sessions: int = 5,
        max_hold_minutes: int = 60,
        entry_window_minutes: int = 90,
        size: int = 1,
    ):
        self.interval = interval
        self.range_bars = range_bars
        self.breakout_atr_mult = breakout_atr_mult
        self.atr_lookback_sessions = atr_lookback_sessions
        self.max_hold_minutes = max_hold_minutes
        self.entry_window_minutes = entry_window_minutes
        self.size = size

        # Per-session state
        self._session = None
        self._bars_this_session = 0
        self._range_high = None
        self._range_low = None
        self._entry_ts = None
        self._prior_range_widths: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Track session and bar count
        if ctx.session != self._session:
            # Store the prior session's range width
            if self._session is not None and self._range_high is not None:
                self._prior_range_widths.append(self._range_high - self._range_low)
                if len(self._prior_range_widths) > self.atr_lookback_sessions:
                    self._prior_range_widths.pop(0)
            self._session = ctx.session
            self._bars_this_session = 0
            self._range_high = None
            self._range_low = None
            self._entry_ts = None

        # Only act on our interval
        if self.interval not in ctx.bars:
            return

        self._bars_this_session += 1

        # Build opening range from first `range_bars` bars
        if self._bars_this_session <= self.range_bars:
            bar = ctx.bars[self.interval]
            if self._range_high is None:
                self._range_high = bar.high
                self._range_low = bar.low
            else:
                self._range_high = max(self._range_high, bar.high)
                self._range_low = min(self._range_low, bar.low)
            return  # still building the range

        # We should have a range by now
        if self._range_high is None or self._range_low is None:
            return

        # Manage existing position: check deadline
        if ctx.position:
            if self._entry_ts is not None:
                elapsed_minutes = (ctx.ts - self._entry_ts) / 60_000_000_000
                if elapsed_minutes >= self.max_hold_minutes:
                    ctx.cancel_all()
                    ctx.close()
                    self._entry_ts = None
            return  # let bracket manage the exit otherwise

        # No position, no working orders -- look for a breakout
        if ctx.working_order_ids:
            return

        if not ctx.actionable:
            return

        bar = ctx.bars[self.interval]
        range_width = self._range_high - self._range_low
        if range_width <= 0:
            return

        # Check time window: only enter within entry_window_minutes of session open
        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.entry_window_minutes:
            return

        # Compute breakout threshold from recent range widths
        if len(self._prior_range_widths) >= self.atr_lookback_sessions:
            avg_range = sum(self._prior_range_widths) / len(self._prior_range_widths)
            threshold = self.breakout_atr_mult * avg_range
        else:
            # Not enough history: fall back to a fraction of the current range
            threshold = self.breakout_atr_mult * range_width

        # Breakout to the upside
        if bar.close > self._range_high + threshold:
            ctx.buy(
                size=self.size,
                stop=self._range_low,
                tag="range-breakout-long",
            )
            self._entry_ts = ctx.ts
        # Breakout to the downside
        elif bar.close < self._range_low - threshold:
            ctx.sell(
                size=self.size,
                stop=self._range_high,
                tag="range-breakout-short",
            )
            self._entry_ts = ctx.ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
