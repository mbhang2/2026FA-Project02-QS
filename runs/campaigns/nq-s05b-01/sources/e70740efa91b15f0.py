"""Short-side opening reversal strategy for NQ futures, v4.

Prior trials all had negative scores, with the best at -0.0299. The
pattern may be weak or non-existent, but we must continue investigating.
This version makes several changes:

1. Uses a 1-minute interval instead of 30 seconds to reduce noise and
   transaction costs.
2. Extends the opening window to 60 minutes to capture more potential
   reversal setups.
3. Simplifies the signal: enter short when a bar closes above the prior
   lookback high, without additional filters that may have been too
   restrictive.
4. Uses a fixed stop/target distance based on the average true range of
   the lookback window, scaled by a multiplier.
5. Adds a cooldown period to avoid overtrading.

The ring buffer approach is retained to avoid WindowTooShort errors.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 60,
        lookback: int = 5,
        atr_mult: float = 1.0,
        size: int = 1,
        cooldown_bars: int = 5,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.atr_mult = atr_mult
        self.size = size
        self.cooldown_bars = cooldown_bars
        self._session = None
        self._bars_since_trade = 0
        self._recent_highs: deque[float] = deque(maxlen=lookback)
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
            self._bars_since_trade = 0
            self._recent_highs.clear()
            self._recent_ranges.clear()

        self._bars_since_trade += 1
        if self._bars_since_trade < self.cooldown_bars:
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

        # Update ring buffers with prior bars.
        if len(self._recent_highs) == self.lookback:
            prior_high = max(self._recent_highs)
            avg_range = sum(self._recent_ranges) / len(self._recent_ranges)

            # Signal: close above prior high.
            if last.close > prior_high and avg_range > 0:
                risk = self.atr_mult * avg_range
                if risk <= 0:
                    risk = 1.0
                ctx.sell(
                    size=self.size,
                    stop_offset=risk,
                    target_offset=risk,
                    tag="opening-reversal-short",
                )
                self._bars_since_trade = 0
                return

        self._recent_highs.append(last.high)
        self._recent_ranges.append(last.high - last.low)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
