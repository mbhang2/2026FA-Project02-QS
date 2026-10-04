"""Short-side opening reversal strategy for NQ futures, v2.

Adapted from the paper's opening-range reversal idea: after an early-session
rise, sell short with a symmetric 1:1 stop and target. The strategy is
short-only, enters on a price rise in the opening minutes, and uses a fixed
point distance for both stop and target.

Changes from prior version:
- Avoid iterating over history slices that exceed the engine's 512-bar window.
  The previous crash came from `history[-(self.lookback + 1):-1]` when the
  history was longer than the window; now we use `history.last(lookback + 1)`
  which is window-safe.
- Add a minimum absolute rise filter to avoid taking trades on tiny noise bars.
- Require the current bar to close in the upper half of its range, a stronger
  expression of upward pressure.
- Keep the rest of the logic: first signal per session, opening window only,
  symmetric 1:1 bracket.
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
        min_rise: float = 0.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_points = risk_points
        self.min_rise = min_rise
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

        # Window-safe access to the most recent bars.
        window = history.last(self.lookback + 1)
        last = window[-1]
        prior = window[:-1]
        if not prior:
            return

        prior_high = max(b.high for b in prior)

        # Signal: current bar closes above the prior lookback high,
        # closes up by at least min_rise, and closes in the upper half
        # of its range.
        if last.close <= prior_high:
            return
        if last.close - last.open < self.min_rise:
            return
        bar_range = last.high - last.low
        if bar_range <= 0:
            return
        if (last.close - last.low) < 0.5 * bar_range:
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
