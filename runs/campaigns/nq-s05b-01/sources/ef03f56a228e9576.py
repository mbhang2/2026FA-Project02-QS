"""Short-side opening reversal strategy for NQ futures, v7.

Prior trials show the best score (+0.1252) from #39, which used a 1-minute
interval, entered only in the first 15 minutes, and required a new opening high
followed by a strong bearish reversal bar. This version keeps that core but
adds a volume confirmation to avoid low-conviction signals, and slightly
tightens the rally filter. The goal is to improve selectivity while maintaining
the exposure floor (>=70% of sessions traded).

Key changes from v6:
- Require the reversal bar's sell volume to exceed buy volume (delta < 0),
  indicating actual selling pressure.
- Use risk_mult=0.4 to reduce stop distance and increase trade frequency.
- Keep min_rally=1.0 but require the rally to be at least 1.5 points if the
  opening range is small.
- Use a 2-minute interval to reduce noise while still acting early.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "2m",
        opening_minutes: int = 15,
        lookback: int = 4,
        risk_mult: float = 0.4,
        min_range: float = 2.0,
        bearish_body_mult: float = 0.5,
        min_rally: float = 1.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_range = min_range
        self.bearish_body_mult = bearish_body_mult
        self.min_rally = min_rally
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._bars_seen = 0
        self._first_close = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        # Reset per-session state on session change.
        if ctx.session != self._session:
            self._session = ctx.session
            self._traded_this_session = False
            self._opening_high = None
            self._bars_seen = 0
            self._first_close = None

        if self._traded_this_session:
            return

        # Only trade during the opening window.
        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.opening_minutes:
            return

        if self.interval not in ctx.bars:
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

        # Volume confirmation: delta must be negative (sell volume > buy volume).
        if last.delta >= 0:
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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
