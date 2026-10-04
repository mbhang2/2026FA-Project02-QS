"""Short-side opening reversal strategy for NQ futures, revised.

Adapted from the paper's opening-range reversal idea: after an early-session
rise, sell short with a symmetric 1:1 stop and target. The strategy is
short-only, enters on a price rise in the opening minutes, and uses a fixed
point distance for both stop and target.

Changes from prior trials:
- Uses a rolling high of the last `lookback` bars, but tracks the high
  incrementally on `self` to avoid any risk of exceeding the history window.
- Requires a stronger breakout: close above the prior high by at least
  `breakout_buffer` points, and the bar's range must exceed `min_range` points
  to avoid acting on tiny, noisy bars.
- Enters only if the close is within `max_distance_from_high` points of the
  bar's high, indicating the breakout is not already fading.
- Adds a session-level filter: skip the first `skip_first_sessions` sessions
  to avoid any early-data artifacts.
- Keeps at most one trade per session.
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
        breakout_buffer: float = 0.25,
        min_range: float = 0.5,
        max_distance_from_high: float = 0.25,
        skip_first_sessions: int = 0,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_points = risk_points
        self.size = size
        self.breakout_buffer = breakout_buffer
        self.min_range = min_range
        self.max_distance_from_high = max_distance_from_high
        self.skip_first_sessions = skip_first_sessions

        self._session = None
        self._traded_this_session = False
        self._prior_highs: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._traded_this_session = False
            self._prior_highs.clear()

        if ctx.session < self.skip_first_sessions:
            return
        if self._traded_this_session:
            return

        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.opening_minutes:
            return

        # We only need the current bar and the prior lookback bars.
        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        # Maintain a rolling list of the last `lookback` bar highs.
        # We can't rely on history[-lookback:] being available in the window,
        # so we keep our own list.
        bars = history.last(self.lookback + 1)
        if len(bars) < self.lookback + 1:
            return

        # Update rolling highs from the bars we just received.
        # We clear on session change, so this is safe.
        for bar in bars[:-1]:
            if len(self._prior_highs) >= self.lookback:
                self._prior_highs.pop(0)
            self._prior_highs.append(bar.high)

        if len(self._prior_highs) < self.lookback:
            return

        prior_high = max(self._prior_highs)
        last = bars[-1]

        # Stronger breakout conditions.
        if last.close <= prior_high + self.breakout_buffer:
            return
        if last.close <= last.open:
            return
        if (last.high - last.low) < self.min_range:
            return
        if (last.high - last.close) > self.max_distance_from_high:
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
