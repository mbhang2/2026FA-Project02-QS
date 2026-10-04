"""Short-side opening reversal for NQ futures, v7.

This version simplifies the entry to a fixed-time reversal at the close of the
third 5-minute bar (15 minutes after the open). The opening high is the highest
high of the first three bars. We short if the current bar is bearish (close <
open), the opening range is at least 1.0 point, and the rally from the first
bar's close to the opening high is at least 0.5 point. Risk is the full distance
from the entry to the opening high, with a symmetric target the same distance
below the entry. This is a 1:1 risk/reward structure as required.

Compared to v6, this uses 5-minute bars instead of 1-minute, a fixed entry at
the 15-minute mark rather than waiting for a bearish bar after an arbitrary
lookback, and a full-distance stop/target instead of a half-distance one. The
minimum range filter is relaxed from 2.0 to 1.0 points to retain more sessions
while still avoiding very quiet opens.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_minutes: int = 15,
        lookback: int = 3,
        min_rally: float = 0.5,
        min_range: float = 1.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.min_rally = min_rally
        self.min_range = min_range
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._opening_low = None
        self._bars_seen = 0

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
            self._opening_high = None
            self._opening_low = None
            self._bars_seen = 0

        if self._traded_this_session:
            return

        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        self._bars_seen += 1
        last = history[-1]

        # Track opening high/low for the first lookback bars.
        if self._bars_seen <= self.lookback:
            if self._opening_high is None or last.high > self._opening_high:
                self._opening_high = last.high
            if self._opening_low is None or last.low < self._opening_low:
                self._opening_low = last.low

        # Act only at the close of the lookback-th bar.
        if self._bars_seen != self.lookback:
            return

        # Ensure we are still within the opening window (should be exactly at
        # the boundary for 5m bars and lookback=3).
        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.opening_minutes + 1e-9:
            return

        if self._opening_high is None or self._opening_low is None:
            return

        opening_range = self._opening_high - self._opening_low
        if opening_range < self.min_range:
            return

        first_close = history[-self.lookback].close
        rally = self._opening_high - first_close
        if rally < self.min_rally:
            return

        # Require a bearish reversal bar.
        if last.close >= last.open:
            return

        risk = self._opening_high - last.close
        if risk <= 0:
            return

        ctx.sell(
            size=self.size,
            stop_offset=risk,
            target_offset=risk,
            tag="opening-reversal-short",
        )
        self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
