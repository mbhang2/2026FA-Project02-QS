"""Short-side opening reversal strategy for NQ futures, v3.

The previous trials all traded too often and too mechanically. The core idea
remains: sell a new high during the opening window with a symmetric 1:1
bracket. This version tightens the signal to trade only when the breakout
bar is strong relative to recent bars, and it requires a retest of the
breakout level before entry. The goal is to reduce the number of low-quality
trades and improve the average outcome.

Changes from v2:
- Signal now requires the bar to close above the prior high by a meaningful
  margin (0.25 points), not just a tick.
- Adds a confirmation step: after a breakout bar, wait for the next bar to
  trade back down to the breakout level before entering short. This filters
  out breakouts that immediately continue higher.
- Uses a 2-minute bar to reduce noise while still acting within the opening
  hour.
- Keeps a small deque of prior highs to avoid slicing history.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "2m",
        opening_minutes: int = 60,
        lookback: int = 6,
        breakout_margin: float = 0.25,
        risk_points: float = 3.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.breakout_margin = breakout_margin
        self.risk_points = risk_points
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._recent_highs: deque[float] = deque(maxlen=lookback)
        self._pending_breakout = False
        self._breakout_level = 0.0

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
            self._recent_highs.clear()
            self._pending_breakout = False
            self._breakout_level = 0.0

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

        # If we have a pending breakout, see if this bar retests the level.
        if self._pending_breakout:
            if last.low <= self._breakout_level:
                # Retest confirmed: enter short.
                ctx.sell(
                    size=self.size,
                    stop_offset=self.risk_points,
                    target_offset=self.risk_points,
                    tag="opening-reversal-short",
                )
                self._traded_this_session = True
                self._pending_breakout = False
                return
            # If the bar closes well above the breakout level, abandon the setup.
            if last.close > self._breakout_level + self.breakout_margin * 2:
                self._pending_breakout = False
            return

        # Build up the prior-high buffer.
        if len(self._recent_highs) == self.lookback:
            prior_high = max(self._recent_highs)
            # Breakout: close above prior high by a margin, and the bar closes up.
            if (
                last.close > prior_high + self.breakout_margin
                and last.close > last.open
            ):
                self._pending_breakout = True
                self._breakout_level = last.close
                return

        self._recent_highs.append(last.high)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
