"""Investigate whether an outsized move inside a session gives back, with a
rolling volatility baseline and a session-direction filter.

Hypothesis: after a bar whose range is large relative to the recent average
bar range (rolling window), price tends to revert (give back) rather than
continue, especially when the move is against the session's prevailing
direction. We measure "outsized" as the current bar's range exceeding a
multiple of the average range of the last `lookback` bars. We only trade when
the outsized bar's direction is opposite to the session's net change from its
open (i.e., a counter-trend spike). Entry is at market on the bar close,
opposite to the bar's direction, with a bracket sized off the bar's range.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 20,
        range_mult: float = 2.5,
        stop_mult: float = 0.5,
        target_mult: float = 1.5,
        size: int = 1,
        min_time_left_ns: int = 30 * 60 * 1_000_000_000,  # 30 minutes
    ):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.min_time_left_ns = min_time_left_ns

        # Rolling window of recent bar ranges (excluding current bar)
        self._ranges: deque[float] = deque(maxlen=lookback)

        # Session state
        self._session: int | None = None
        self._session_open: float | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset session state on session change
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_open = None
            self._ranges.clear()

        # Only act on our interval's bar close
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Record session open price from the first bar of the session
        if self._session_open is None:
            self._session_open = bar.open

        # Check actionable and time left
        if not ctx.actionable:
            return
        time_left = ctx.session_end - ctx.ts
        if time_left < self.min_time_left_ns:
            return

        # Only one position at a time
        if ctx.position or ctx.working_order_ids:
            return

        # Need enough bars to compute a rolling average range
        if len(self._ranges) < self.lookback:
            # Not enough history yet; just update the rolling window
            self._ranges.append(bar.high - bar.low)
            return

        # Compute average range from the rolling window (previous bars)
        avg_range = sum(self._ranges) / len(self._ranges)
        if avg_range <= 0:
            return

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return

        # Outsized move if current bar range exceeds multiple of rolling average
        if bar_range < self.range_mult * avg_range:
            # Not outsized; update rolling window and return
            self._ranges.append(bar_range)
            return

        # Determine session direction: up if current price above session open
        session_up = bar.close > self._session_open
        bar_up = bar.close > bar.open

        # Only trade if the outsized move is against the session direction
        # (counter-trend spike is more likely to give back)
        if bar_up == session_up:
            # Move is with the session trend; skip
            self._ranges.append(bar_range)
            return

        # Direction: give-back (bar up -> sell, bar down -> buy)
        stop_offset = max(bar_range * self.stop_mult, 0.25)
        target_offset = max(bar_range * self.target_mult, 0.25)

        if bar_up:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-giveback-short",
            )
        else:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-giveback-long",
            )

        # Update rolling window after signal (include current bar for next time)
        self._ranges.append(bar_range)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
