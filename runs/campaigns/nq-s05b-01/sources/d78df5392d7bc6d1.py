"""Short-side opening reversal strategy for NQ futures, v4.

Prior trials show the symmetric 1:1 bracket is bleeding out through
commissions and adverse fills. The best score so far (#11) used a 30m
window with a 60s interval and traded 333 times, losing -0.0299.

This version keeps the same core signal but tightens execution:
- 30-minute opening window, 60s bars for stability.
- Signal: bar closes above prior 10-bar high, is an up bar, body above
  median, and average range > 0.
- Risk is 1.5x average range, capped at 2.0 points to avoid oversized
  stops in volatile opens, and floored at 0.5 points to avoid noise.
- Adds a minimum close-above-high distance of 0.25 points, so marginal
  breakouts are filtered out.
- Trades only once per session.

The cap and floor on risk are new and should reduce the tail losses that
have plagued earlier versions while preserving the reversal edge.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "60s",
        opening_minutes: int = 30,
        lookback: int = 10,
        atr_mult: float = 1.5,
        min_risk: float = 0.5,
        max_risk: float = 2.0,
        min_breakout: float = 0.25,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.atr_mult = atr_mult
        self.min_risk = min_risk
        self.max_risk = max_risk
        self.min_breakout = min_breakout
        self.size = size
        self._session = None
        self._traded_this_session = False
        self._recent_highs: deque[float] = deque(maxlen=lookback)
        self._recent_bodies: deque[float] = deque(maxlen=lookback)
        self._recent_ranges: deque[float] = deque(maxlen=lookback)

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
            self._recent_highs.clear()
            self._recent_bodies.clear()
            self._recent_ranges.clear()

        if self._traded_this_session:
            return

        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.opening_minutes:
            return

        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]

        if len(self._recent_highs) == self.lookback:
            prior_high = max(self._recent_highs)
            median_body = sorted(self._recent_bodies)[len(self._recent_bodies) // 2]
            avg_range = sum(self._recent_ranges) / len(self._recent_ranges)

            body = abs(last.close - last.open)
            breakout = last.close - prior_high

            if (
                breakout >= self.min_breakout
                and last.close > last.open
                and body > median_body
                and avg_range > 0
            ):
                risk = self.atr_mult * avg_range
                risk = max(self.min_risk, min(self.max_risk, risk))
                ctx.sell(
                    size=self.size,
                    stop_offset=risk,
                    target_offset=risk,
                    tag="opening-reversal-short",
                )
                self._traded_this_session = True
                return

        self._recent_highs.append(last.high)
        self._recent_bodies.append(abs(last.close - last.open))
        self._recent_ranges.append(last.high - last.low)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
