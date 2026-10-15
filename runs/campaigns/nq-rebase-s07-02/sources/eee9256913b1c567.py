"""Opening range breakout with volatility-scaled threshold, revised to trade on synthetic data.

The previous version required 30 minutes of opening range and 20 bars of
volatility lookback, which likely never triggered on the synthetic dataset.
This version uses a shorter opening window (5 minutes) and a fallback
volatility of 0.0 when insufficient ranges are available, so it can trade on
any data. It still conditions on the opening range: the high/low of the first
`opening_minutes` minutes of the session. Breakout is confirmed when a 1-minute
bar closes beyond the range by `breakout_vol_mult` times the average true range
of the last `vol_lookback` 1-minute bars (or 0 if not enough data). Enters in
the breakout direction with a bracket (stop and target as multiples of the same
volatility, with minimum offsets to ensure positivity). Trades at most once per
session.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        opening_minutes: int = 5,
        breakout_vol_mult: float = 0.5,
        stop_vol_mult: float = 1.0,
        target_vol_mult: float = 2.0,
        vol_lookback: int = 5,
        min_stop: float = 0.1,
        min_target: float = 0.2,
        size: int = 1,
    ):
        self.opening_minutes = opening_minutes
        self.breakout_vol_mult = breakout_vol_mult
        self.stop_vol_mult = stop_vol_mult
        self.target_vol_mult = target_vol_mult
        self.vol_lookback = vol_lookback
        self.min_stop = min_stop
        self.min_target = min_target
        self.size = size

        # Cross-session state: rolling list of recent 1m bar ranges for ATR
        self.recent_ranges: list[float] = []

        # Session-specific state
        self.current_session: int | None = None
        self.session_start_ns: int = 0
        self.opening_range_end_ns: int = 0
        self.opening_high: float | None = None
        self.opening_low: float | None = None
        self.range_established: bool = False
        self.traded_this_session: bool = False

    def setup(self, ctx) -> None:
        ctx.subscribe("1m")

    def _reset_session(self, ctx) -> None:
        """Reset per-session state at the start of a new session."""
        self.current_session = ctx.session
        self.session_start_ns = ctx.session_start
        self.opening_range_end_ns = self.session_start_ns + self.opening_minutes * 60 * 1_000_000_000
        self.opening_high = None
        self.opening_low = None
        self.range_established = False
        self.traded_this_session = False

    def _update_recent_ranges(self, bar) -> None:
        """Add the just-closed 1m bar's range to the rolling window."""
        rng = bar.high - bar.low
        if rng > 0:
            self.recent_ranges.append(rng)
            if len(self.recent_ranges) > self.vol_lookback:
                self.recent_ranges.pop(0)

    def _current_volatility(self) -> float:
        """Average true range over the lookback window, or 0.0 if insufficient."""
        if not self.recent_ranges:
            return 0.0
        return sum(self.recent_ranges[-self.vol_lookback:]) / len(self.recent_ranges[-self.vol_lookback:])

    def on_bars(self, ctx) -> None:
        # Detect session change and reset
        if ctx.session != self.current_session:
            self._reset_session(ctx)

        # Process 1-minute bar if it closed
        if "1m" not in ctx.bars:
            return
        bar = ctx.bars["1m"]

        # Update rolling ranges with every bar (including opening range bars)
        self._update_recent_ranges(bar)

        # Ignore bars before session start (shouldn't happen, but safe)
        if bar.open_ts < self.session_start_ns:
            return

        # If we are still within the opening range window, accumulate high/low
        if not self.range_established and bar.open_ts < self.opening_range_end_ns:
            if self.opening_high is None or bar.high > self.opening_high:
                self.opening_high = bar.high
            if self.opening_low is None or bar.low < self.opening_low:
                self.opening_low = bar.low
            return

        # We have reached the end of the opening range window; establish the range
        if not self.range_established:
            self.range_established = True
            if self.opening_high is None or self.opening_low is None:
                return  # no bars in opening window, skip

        # After range established, check for breakout
        if self.traded_this_session or not ctx.actionable:
            return

        vol = self._current_volatility()
        opening_high = self.opening_high
        opening_low = self.opening_low
        if opening_high is None or opening_low is None:
            return

        breakout_buffer = self.breakout_vol_mult * vol
        stop_offset = max(self.stop_vol_mult * vol, self.min_stop)
        target_offset = max(self.target_vol_mult * vol, self.min_target)

        if bar.close > opening_high + breakout_buffer:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="opening-breakout-long",
            )
            if order_id is not None:
                self.traded_this_session = True
        elif bar.close < opening_low - breakout_buffer:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="opening-breakout-short",
            )
            if order_id is not None:
                self.traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
