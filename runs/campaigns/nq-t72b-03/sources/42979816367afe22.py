"""Give-back with adaptive threshold and session-level context.

After an outsized bar, enter opposite with a bracket. The "outsized" threshold
is not fixed: it is the 80th percentile of bar ranges observed so far in the
session, so the strategy adapts to the session's own volatility. A minimum
session move and a time-of-day filter keep it out of the open and close.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        min_bars: int = 12,
        percentile: float = 0.80,
        stop_mult: float = 0.6,
        target_mult: float = 1.4,
        size: int = 1,
        min_time_left_ns: int = 45 * 60 * 1_000_000_000,
        session_open_skip_ns: int = 20 * 60 * 1_000_000_000,
    ):
        self.interval = interval
        self.min_bars = min_bars
        self.percentile = percentile
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.min_time_left_ns = min_time_left_ns
        self.session_open_skip_ns = session_open_skip_ns

        self._session = None
        self._bars_this_session = 0
        self._ranges: list[float] = []
        self._session_open_price = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._ranges = []
            self._session_open_price = None

        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        if self._session_open_price is None:
            self._session_open_price = bar.open

        self._bars_this_session += 1
        bar_range = bar.high - bar.low
        self._ranges.append(bar_range)

        if not ctx.actionable:
            return

        if ctx.ts - ctx.session_start < self.session_open_skip_ns:
            return

        if self._bars_this_session < self.min_bars:
            return

        if ctx.session_end - ctx.ts < self.min_time_left_ns:
            return

        if ctx.position or ctx.working_order_ids:
            return

        if bar.close == bar.open:
            return

        # Adaptive threshold from the session's own ranges so far
        sorted_ranges = sorted(self._ranges)
        idx = int(self.percentile * (len(sorted_ranges) - 1))
        threshold = sorted_ranges[idx]
        if threshold <= 0:
            return

        if bar_range < threshold:
            return

        # Session must have moved at least 1.5x the adaptive threshold
        session_move = abs(bar.close - self._session_open_price)
        if session_move < 1.5 * threshold:
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
