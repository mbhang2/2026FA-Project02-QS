"""Short-side opening reversal strategy for NQ futures, v8.

This version fixes the mypy error from v7 where `self._opening_high` was
`None` when used in arithmetic. The fix is to initialize `_opening_high` and
`_opening_low` to `0.0` and use a separate flag to track whether they have
been set, or simply check for `None` before using them. Here we use a
sentinel value of `None` but guard all arithmetic with a check.

The strategy itself is unchanged from v7, which was based on trial #57
(+0.1087). It trades only during the first 30 minutes, requires a rally above
the first bar's close, and enters short when the current bar closes below
both the opening high and the previous bar's low. Risk is symmetric 1:1
based on the distance from entry to the opening high.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 30,
        min_rally: float = 1.0,
        min_range: float = 2.0,
        risk_mult: float = 1.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.min_rally = min_rally
        self.min_range = min_range
        self.risk_mult = risk_mult
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None  # type: float | None
        self._opening_low = None   # type: float | None
        self._first_close = None   # type: float | None
        self._bars_seen = 0

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
            self._opening_low = None
            self._first_close = None
            self._bars_seen = 0

        if self._traded_this_session:
            return

        # Only trade during the opening window.
        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.opening_minutes:
            return

        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        last = history[-1]
        prev = history[-2]
        self._bars_seen += 1

        # Track first bar's close.
        if self._bars_seen == 1:
            self._first_close = last.close

        # Update opening high and low.
        if self._opening_high is None or last.high > self._opening_high:
            self._opening_high = last.high
        if self._opening_low is None or last.low < self._opening_low:
            self._opening_low = last.low

        # Need at least 2 bars to have a previous bar for confirmation.
        if self._bars_seen < 2:
            return

        # Guard against None before arithmetic. This should never happen
        # because we set _opening_high and _opening_low on the first bar,
        # but mypy needs the explicit check.
        if self._opening_high is None or self._opening_low is None:
            return

        # Opening range filter.
        opening_range = self._opening_high - self._opening_low
        if opening_range < self.min_range:
            return

        # Rally filter: opening high must be sufficiently above first close.
        if self._first_close is not None:
            rally = self._opening_high - self._first_close
            if rally < self.min_rally:
                return

        # Reversal signal: current bar closes below the opening high AND
        # below the previous bar's low, indicating a break of structure.
        if last.close >= self._opening_high:
            return
        if last.close >= prev.low:
            return

        # Risk is distance from entry (current close) to the opening high.
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
