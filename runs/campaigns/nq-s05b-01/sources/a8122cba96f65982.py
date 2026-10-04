"""Short-side opening reversal strategy for NQ futures, v2.

Adapted from the paper's opening-range reversal idea: after an early-session
rise, sell short with a symmetric 1:1 stop and target.

Changes from the prior version:
- The prior version crashed with WindowTooShort because it sliced
  `history[-(lookback + 1):-1]` on a long history. The engine's history view
  is a bounded window, so iterating or slicing past the window raises.
  This version uses `history.last(self.lookback + 1)` to get the most recent
  bars within the window safely.
- Keeps the same short-only, symmetric-bracket design.
- Adds a session-end safety net: no new entries in the final 15 minutes.
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
        session_close_margin_minutes: int = 15,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_points = risk_points
        self.size = size
        self.session_close_margin_minutes = session_close_margin_minutes
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
        session_elapsed_minutes = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed_minutes > self.opening_minutes:
            return

        # Avoid entering too close to the session close.
        time_left_minutes = (ctx.session_end - ctx.ts) / 60_000_000_000
        if time_left_minutes < self.session_close_margin_minutes:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        # Use history.last() to stay within the bounded window.
        window = history.last(self.lookback + 1)
        if len(window) < self.lookback + 1:
            return

        last = window[-1]
        prior = window[:-1]
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
