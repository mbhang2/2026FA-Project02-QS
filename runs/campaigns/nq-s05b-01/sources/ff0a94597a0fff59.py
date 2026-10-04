"""Short-side opening reversal strategy for NQ futures.

Adapted from the 0DTE hedging pressure hypothesis: if NQ establishes a
meaningful high in the opening minutes of a session, sell that high with a
symmetric 1:1 stop and target. The campaign requires short-only, reversal
entries after a price rise, symmetric risk/reward, and a definition of the
opening period.

Design choices:
- Opening period: first 30 minutes of the session (six 5-minute bars).
- High definition: the session's highest price after at least three bars have
  closed. We require a minimum number of bars so the "high" is not just the
  first tick of the session.
- Entry timing: on the first 5-minute bar close after the opening high is
  established, if price is still near that high (within a small tolerance).
  This captures the reversal attempt early while the high is fresh.
- Risk/reward: symmetric, fixed 1.0 point stop and target on NQ (about 20
  ticks). The paper used a similar small, fixed structure.
- One trade per session, short-only, no overnight holding.
"""

from __future__ import annotations


class Strategy:
    """Short-side opening reversal with symmetric fixed bracket."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_bars: int = 6,          # 30 minutes of 5m bars
        min_bars_before_high: int = 3,  # require at least this many bars
        near_high_tolerance: float = 0.5,
        stop_points: float = 1.0,
        target_points: float = 1.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.min_bars_before_high = min_bars_before_high
        self.near_high_tolerance = near_high_tolerance
        self.stop_points = stop_points
        self.target_points = target_points
        self.size = size

        # Per-session state
        self._session: int | None = None
        self._bars_this_session: int = 0
        self._session_high: float | None = None
        self._high_bar_index: int | None = None
        self._traded_this_session: bool = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _reset_session(self, session: int) -> None:
        self._session = session
        self._bars_this_session = 0
        self._session_high = None
        self._high_bar_index = None
        self._traded_this_session = False

    def on_bars(self, ctx) -> None:
        # Detect session rollover
        if ctx.session != self._session:
            self._reset_session(ctx.session)

        self._bars_this_session += 1

        # Update session high with the bar that just closed
        history = ctx.history(self.interval)
        if not history:
            return
        bar = history[-1]
        if self._session_high is None or bar.high > self._session_high:
            self._session_high = bar.high
            self._high_bar_index = self._bars_this_session

        # Only act during actionable sessions, within the opening period,
        # and only once per session.
        if not ctx.actionable:
            return
        if self._traded_this_session:
            return
        if self._bars_this_session > self.opening_bars:
            return

        # Need enough bars to have a meaningful high
        if self._bars_this_session < self.min_bars_before_high:
            return

        # The high must have been set at least one bar ago, so we are
        # reacting to an established high, not the bar that just made it.
        if self._high_bar_index is None or self._high_bar_index >= self._bars_this_session:
            return

        # Current price must be near the session high (reversal attempt)
        if ctx.last_price < self._session_high - self.near_high_tolerance:
            return

        # Short with symmetric bracket
        ctx.sell(
            size=self.size,
            stop_offset=self.stop_points,
            target_offset=self.target_points,
            tag="opening-reversal-short",
        )
        self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
