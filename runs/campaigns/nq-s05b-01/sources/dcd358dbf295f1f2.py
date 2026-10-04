"""Short-side opening reversal strategy for NQ futures.

Adapted from the paper's opening-range reversal idea: after an early-session
rise, sell short with a symmetric 1:1 stop and target. The strategy is
short-only, enters on a price rise in the opening minutes, and uses a fixed
point distance for both stop and target.

Design:
- Subscribe to 1-minute bars for fine-grained opening action.
- Opening window: first 60 minutes of the session (configurable).
- A "high" is established when the bar's close is above the prior 5-bar high,
  and the bar closes up. We only take the first such signal per session.
- Enter short at the next bar's open via a market order with a symmetric
  bracket: stop and target both at `risk_points` from the entry.
- The position is protected from the instant it fills, and we take at most one
  trade per session.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 60,
        lookback: int = 5,
        risk_points: float = 3.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_points = risk_points
        self.size = size
        self._session = None
        self._traded_this_session = False

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

        if self._traded_this_session:
            return

        # Only trade during the opening window.
        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.opening_minutes:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        # The most recent closed bar and the prior bars for the high.
        last = history[-1]
        prior = history[-(self.lookback + 1):-1]
        if not prior:
            return

        prior_high = max(b.high for b in prior)
        # Signal: current bar closes above the prior lookback high, and closes up.
        if last.close <= prior_high or last.close <= last.open:
            return

        # Short with symmetric stop and target.
        ctx.sell(
            size=self.size,
            stop_offset=self.risk_points,
            target_offset=self.risk_points,
            tag="opening-reversal-short",
        )
        self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
