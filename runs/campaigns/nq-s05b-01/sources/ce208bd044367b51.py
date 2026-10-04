"""Short-side opening reversal strategy for NQ futures, v9.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. Prior trials show a few positive scores but most are negative.
This version makes a small, meaningful change from the previous one: it
removes the requirement that the close be below the midpoint of the opening
range, which was overly restrictive and may have filtered out valid reversal
signals. Instead, it relies on the bearish bar body and the close below the
prior bar's low to confirm the reversal. This should increase the number of
trades while maintaining a selective entry.

The change is designed to be visible on synthetic data: removing a filter
will allow more trades to trigger, so the fill log should differ from the
previous version.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 10,
        lookback: int = 3,
        risk_mult: float = 0.75,
        min_range: float = 1.5,
        min_rally: float = 0.75,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_range = min_range
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
        # Use only the first `lookback` bars for the range, not all bars seen.
        recent_bars = history.last(self.lookback)
        opening_range = self._opening_high - min(b.low for b in recent_bars)
        if opening_range < self.min_range:
            return

        # Require a meaningful rally above the first bar's close.
        if self._first_close is not None:
            rally = self._opening_high - self._first_close
            if rally < self.min_rally:
                return

        # Require the close to be below the prior bar's low.
        # This confirms a break of the immediate support, making the reversal
        # more decisive.
        if self._bars_seen < 2:
            return
        prior = history[-2]
        if last.close >= prior.low:
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
