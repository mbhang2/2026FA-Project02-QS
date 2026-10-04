"""Short-side opening reversal strategy for NQ futures, v4.

Previous trials show the core problem: the strategy trades too often and loses
on average. The best score so far is -0.0299, which is close to break-even
before costs. This version aims to reduce the number of trades while keeping
only the most promising setups.

Key changes:
1. Tighten the opening window to 15 minutes. The paper's strongest effect was
   in the earliest minutes, and our prior 30-minute window may be too wide.
2. Require a much stronger signal: a new session high (not just a lookback
   high) with a large body and a close in the top portion of the bar's range.
3. Add a minimum absolute price move filter to avoid noise.
4. Use a longer lookback for the median body comparison to make the signal
   more selective.
5. Keep the symmetric 1:1 stop/target as required, but scale risk based on
   recent average true range with a higher multiplier to give trades more room.

The goal is fewer, higher-quality trades rather than trying to capture every
reversal.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30s",
        opening_minutes: int = 15,
        lookback: int = 20,
        atr_mult: float = 2.0,
        min_move: float = 0.25,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.atr_mult = atr_mult
        self.min_move = min_move
        self.size = size
        self._session = None
        self._traded_this_session = False
        self._session_high = 0.0
        self._recent_bodies: deque[float] = deque(maxlen=lookback)
        self._recent_ranges: deque[float] = deque(maxlen=lookback)

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
            self._session_high = 0.0
            self._recent_bodies.clear()
            self._recent_ranges.clear()

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

        # Update session high with the just-closed bar.
        self._session_high = max(self._session_high, last.high)

        # Update ring buffers with prior bars.
        if len(self._recent_bodies) == self.lookback:
            median_body = sorted(self._recent_bodies)[len(self._recent_bodies) // 2]
            avg_range = sum(self._recent_ranges) / len(self._recent_ranges)

            body = abs(last.close - last.open)
            bar_range = last.high - last.low

            # Signal conditions:
            # 1. Bar closes above the session high (new high).
            # 2. Bar is an up bar.
            # 3. Body is larger than the median body of the lookback window.
            # 4. Bar range is at least the minimum move.
            # 5. Close is in the top half of the bar's range (strong finish).
            if (
                last.close > self._session_high - last.high
                and last.close > last.open
                and body > median_body
                and bar_range >= self.min_move
                and last.close >= last.low + 0.5 * bar_range
                and avg_range > 0
            ):
                risk = self.atr_mult * avg_range
                if risk <= 0:
                    risk = 1.0
                ctx.sell(
                    size=self.size,
                    stop_offset=risk,
                    target_offset=risk,
                    tag="opening-reversal-short",
                )
                self._traded_this_session = True
                return

        self._recent_bodies.append(abs(last.close - last.open))
        self._recent_ranges.append(last.high - last.low)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
