"""Short-side opening reversal strategy for NQ futures, v7.

The campaign investigates whether a short-side reversal pattern, concentrated
in the opening minutes of the session, exists in NQ futures. Prior trials have
been mixed, with the best score +0.1252 from trial #39. This version attempts
to improve by:

1. Restricting trades to the first 20 minutes of the session.
2. Using a 1-minute bar interval.
3. Defining the opening high as the highest high of the first N bars, and
   entering short when price closes below that high after having made a new
   high (a reversal confirmation).
4. Using a fixed risk based on the opening range rather than an ATR.
5. Adding a minimum opening range filter to avoid trading in very quiet
   sessions.
6. Requiring the reversal bar to be a relatively strong down bar.
7. Only trading if the opening high is above the first bar's close by at
   least a minimum amount.
8. Adding a time-based exit: if the position is still open after a set number
   of bars, exit at the market. This is implemented by tracking the bar count
   at entry and closing from on_bars when the holding period expires.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 20,
        lookback: int = 6,
        risk_mult: float = 0.5,
        min_range: float = 2.5,
        bearish_body_mult: float = 0.5,
        min_rally: float = 1.5,
        max_hold_bars: int = 10,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_range = min_range
        self.bearish_body_mult = bearish_body_mult
        self.min_rally = min_rally
        self.max_hold_bars = max_hold_bars
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._bars_seen = 0
        self._first_close = None
        self._entry_bar_count = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset per-session state on session change.
        if ctx.session != self._session:
            self._session = ctx.session
            self._traded_this_session = False
            self._opening_high = None
            self._bars_seen = 0
            self._first_close = None
            self._entry_bar_count = 0

        if self.interval not in ctx.bars:
            return

        # Time-based exit: if we have a position and have held it too long,
        # close it. We do this before the entry logic so the exit happens
        # even if the entry conditions are no longer met.
        if ctx.position:
            self._entry_bar_count += 1
            if self._entry_bar_count >= self.max_hold_bars:
                ctx.close()
            return

        if self._traded_this_session:
            return

        # Only trade during the opening window.
        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.opening_minutes:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]
        self._bars_seen += 1

        # Track the first bar's close for the rally filter.
        if self._bars_seen == 1:
            self._first_close = last.close

        # Need enough bars to establish an opening high.
        if self._bars_seen < self.lookback:
            if self._opening_high is None or last.high > self._opening_high:
                self._opening_high = last.high
            return

        # Update opening high with the current bar.
        if last.high > self._opening_high:
            self._opening_high = last.high

        # Signal: price made a new opening high and then closed below it.
        # Require a meaningful opening range to filter quiet sessions.
        opening_range = self._opening_high - min(
            b.low for b in history.last(self._bars_seen)
        )
        if opening_range < self.min_range:
            return

        # Require a meaningful rally above the first bar's close.
        if self._first_close is not None:
            rally = self._opening_high - self._first_close
            if rally < self.min_rally:
                return

        # Require a bearish reversal bar: close below open and a body that is
        # at least a fraction of the bar's range.
        bar_range = last.high - last.low
        if bar_range <= 0:
            return
        body = last.open - last.close
        if last.close >= last.open or body < self.bearish_body_mult * bar_range:
            return

        # Risk is a fraction of the distance from the high to the close.
        risk = self.risk_mult * (self._opening_high - last.close)
        if risk <= 0:
            return

        ctx.sell(
            size=self.size,
            stop_offset=risk,
            target_offset=risk,
            tag="opening-reversal-short",
        )
        self._traded_this_session = True
        self._entry_bar_count = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
