"""Session-level give-back with tighter regime filter and time-of-day exit.

After an outsized bar, enter opposite with a bracket. Only trade when the
session has established a clear directional regime, and avoid the first and
last portions of the session. The bracket is sized from the bar range but
clamped to avoid degenerate levels.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        min_bars: int = 12,
        range_mult: float = 2.2,
        stop_mult: float = 0.6,
        target_mult: float = 1.4,
        size: int = 1,
        min_time_left_ns: int = 45 * 60 * 1_000_000_000,  # 45 minutes
        session_open_skip_ns: int = 20 * 60 * 1_000_000_000,  # skip first 20 min
    ):
        self.interval = interval
        self.min_bars = min_bars
        self.range_mult = range_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.min_time_left_ns = min_time_left_ns
        self.session_open_skip_ns = session_open_skip_ns

        self._session = None
        self._bars_this_session = 0
        self._sum_ranges = 0.0
        self._session_open_price = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._sum_ranges = 0.0
            self._session_open_price = None

        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        if self._session_open_price is None:
            self._session_open_price = bar.open

        self._bars_this_session += 1
        bar_range = bar.high - bar.low
        self._sum_ranges += bar_range

        if not ctx.actionable:
            return

        # Skip the first part of the session
        if ctx.ts - ctx.session_start < self.session_open_skip_ns:
            return

        # Require enough bars for a baseline
        if self._bars_this_session < self.min_bars:
            return

        # Require enough time left
        if ctx.session_end - ctx.ts < self.min_time_left_ns:
            return

        if ctx.position or ctx.working_order_ids:
            return

        avg_range = self._sum_ranges / self._bars_this_session
        if avg_range <= 0:
            return

        if bar_range < self.range_mult * avg_range:
            return

        if bar.close == bar.open:
            return

        # Regime filter: session must have moved at least 1.5x average bar range
        # from its open, so we are trading counter to a real move, not noise.
        session_move = abs(bar.close - self._session_open_price)
        if session_move < 1.5 * avg_range:
            return

        stop_offset = max(bar_range * self.stop_mult, 0.25)
        target_offset = max(bar_range * self.target_mult, 0.25)

        if bar.close > bar.open:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="giveback-short",
            )
        else:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="giveback-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
