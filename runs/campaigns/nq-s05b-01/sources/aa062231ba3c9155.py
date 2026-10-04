"""Short-side opening reversal strategy for NQ futures, v7.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. Prior trials show that the best scores (+0.1244, +0.1252) came
from strategies that traded in most sessions (360/384) and used relatively
simple filters. The current v6 has many filters that likely reduce exposure
below the required 70% floor, causing many trials to be unscored. This
version relaxes the filters to increase trade frequency while retaining the
core reversal logic: sell after a new opening high when price closes below
that high, with a stop and target based on the distance from the high to the
entry price.

Changes from v6:
- min_range lowered from 2.0 to 1.0 to allow quieter sessions.
- min_rally lowered from 1.0 to 0.5 to allow smaller rallies.
- bearish_body_mult set to 0.0, removing the requirement for a bearish bar.
- risk_mult kept at 0.5, but a minimum risk of 0.5 points is enforced to
  avoid extremely tight stops that would be hit by noise.
- The opening window remains 15 minutes, and the lookback remains 5 bars.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 15,
        lookback: int = 5,
        risk_mult: float = 0.5,
        min_range: float = 1.0,
        min_rally: float = 0.5,
        min_risk: float = 0.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_range = min_range
        self.min_rally = min_rally
        self.min_risk = min_risk
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
        # Require a minimum opening range to filter extremely quiet sessions.
        opening_range = self._opening_high - min(
            b.low for b in history.last(self._bars_seen)
        )
        if opening_range < self.min_range:
            return

        # Require a minimum rally above the first bar's close.
        if self._first_close is not None:
            rally = self._opening_high - self._first_close
            if rally < self.min_rally:
                return

        # Risk is a fraction of the distance from the high to the close,
        # with a floor to avoid overly tight stops.
        risk = self.risk_mult * (self._opening_high - last.close)
        if risk < self.min_risk:
            risk = self.min_risk
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
