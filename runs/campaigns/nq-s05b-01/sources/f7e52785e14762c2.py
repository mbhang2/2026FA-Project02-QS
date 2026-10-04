"""Short-side opening reversal strategy for NQ futures, v4.

Prior trials show the symmetric 1:1 opening reversal idea is unprofitable
on this NQ split. Trial #18 (score -0.0311) used a 5-minute interval with
a 30-minute window and a simple high-of-lookback signal, producing 313
trades. Trial #17 used a 1-minute interval and produced 1710 trades with
a larger loss, suggesting overtrading is harmful.

This version aims to reduce overtrading while preserving the core
short-side reversal logic. It uses a 5-minute interval and a 30-minute
opening window. The signal requires a bar to close above the prior
lookback high, with the bar being an up bar. It also requires a minimum
average true range to filter quiet sessions. The stop and target are
symmetric 1:1, sized at 1.5 times the average true range of the lookback
window. A session trades at most once.

Changes from v3:
- Revert from 30-second bars to 5-minute bars to reduce noise and trade
  frequency (v3 scored -0.3871 with 351 trades).
- Keep the 30-minute opening window, which was used in v3 and v2.
- Simplify the signal: close above prior high and up bar, without the
  median body condition, which may have been too restrictive.
- Use a deque of recent bars to avoid WindowTooShort.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_minutes: int = 30,
        lookback: int = 10,
        atr_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.atr_mult = atr_mult
        self.size = size
        self._session = None
        self._traded_this_session = False
        self._recent_bars: deque = deque(maxlen=lookback)

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
            self._recent_bars.clear()

        if self._traded_this_session:
            return

        session_elapsed_min = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed_min > self.opening_minutes:
            return

        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]

        if len(self._recent_bars) == self.lookback:
            prior_high = max(b.high for b in self._recent_bars)
            avg_range = sum(b.high - b.low for b in self._recent_bars) / self.lookback

            if last.close > prior_high and last.close > last.open and avg_range > 0:
                risk = self.atr_mult * avg_range
                ctx.sell(
                    size=self.size,
                    stop_offset=risk,
                    target_offset=risk,
                    tag="opening-reversal-short",
                )
                self._traded_this_session = True
                return

        self._recent_bars.append(last)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
