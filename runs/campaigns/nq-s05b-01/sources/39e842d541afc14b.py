"""Short-side opening reversal strategy for NQ futures, v6.

Prior trials show the symmetric 1:1 opening reversal idea is unprofitable
on this NQ split. The best score so far is -0.0299 (trial #11), which used
a 30-second interval with a 30-minute window and a high-of-lookback signal.
This version keeps the fast 30-second interval but changes the signal to
focus on a sharper, more defined reversal: it requires the current bar to
close above the prior lookback high AND then immediately reverse downward
within the same bar, i.e., the bar's close is below its midpoint. This
captures a failed breakout, which may be a stronger indication of a
reversal than a simple breakout.

The stop and target remain symmetric 1:1, sized at 1.5 times the average
true range of the lookback window. A session trades at most once.

Changes from v5:
- Switch to 30-second interval for faster signal detection.
- Add a "failed breakout" condition: close above prior high but below the
  bar's midpoint, indicating the breakout was rejected.
- Remove the minimum average true range filter; the failed breakout
  condition may be sufficient to filter noise.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30s",
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
            midpoint = (last.open + last.close) / 2.0

            # Failed breakout: closes above the prior high but below the
            # midpoint of the bar, indicating rejection of the breakout.
            if (
                last.close > prior_high
                and last.close < midpoint
                and avg_range > 0
            ):
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
