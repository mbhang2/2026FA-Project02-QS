"""
Intraday momentum with refined entry timing and volatility filtering.

The prior strategy (score -0.2331) entered at the close of the last 30-minute bar,
which is too late — the exit happens immediately via the bracket. This revision:

1. Enters earlier in the last 30 minutes (around the 60-minute mark from session start)
   to give the position time to develop before the bracket exits
2. Adds volatility filtering: only trade when the first 30-minute bar shows
   meaningful range (not a doji or very small move)
3. Tightens bracket offsets based on measured volatility to improve risk/reward
4. Uses finer bars (5-minute) for more precise timing while still tracking
   30-minute momentum direction
5. Rejects trades when the signal is ambiguous (close ~= open in first bar)

The hypothesis remains: first-30-minute direction predicts last-30-minute direction.
But we enter with more time for the trade to work, and we filter for conviction
in the initial signal.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        fast_interval: str = "5m",
        slow_interval: str = "30m",
        stop_offset: float = 1.0,
        target_offset: float = 1.5,
        size: int = 1,
        min_range_threshold: float = 0.25,
    ):
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_range_threshold = min_range_threshold
        
        # Session tracking
        self._current_session: int | None = None
        self._session_start_ts: int | None = None
        self._first_30m_bar: object | None = None
        self._first_30m_signal: int | None = None  # 1 for up, -1 for down
        self._first_30m_range: float = 0.0
        self._traded_this_session: bool = False
        self._entry_attempted: bool = False

    def setup(self, ctx) -> None:
        """Subscribe to both 5-minute and 30-minute bars."""
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        """
        Trade the first-30-minute signal with refined entry timing.
        
        Logic:
        1. On session change, reset tracking
        2. Capture the first 30-minute bar's direction (must show conviction)
        3. During the last 30 minutes, enter on a 5-minute bar close
           (not immediately at the last 30-minute bar open)
        4. Exit via bracket
        """
        if not ctx.actionable:
            return

        # Handle session transitions
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._session_start_ts = ctx.session_start
            self._first_30m_bar = None
            self._first_30m_signal = None
            self._first_30m_range = 0.0
            self._traded_this_session = False
            self._entry_attempted = False

        # Get 30-minute history to identify first bar
        history_30m = ctx.history(self.slow_interval)
        if len(history_30m) < 1:
            return

        # Capture the first 30-minute bar's direction (with conviction filter)
        if self._first_30m_bar is None and len(history_30m) >= 1:
            first_bar = history_30m[0]
            # Check if this is truly the first bar of the session
            if first_bar.open_ts == self._session_start_ts:
                self._first_30m_bar = first_bar
                # Measure range: high - low
                bar_range = first_bar.high - first_bar.low
                self._first_30m_range = bar_range
                
                # Determine signal with conviction: close must be meaningfully
                # away from open (at least min_range_threshold of the range)
                close_open_diff = first_bar.close - first_bar.open
                if bar_range > 0:
                    conviction = abs(close_open_diff) / bar_range
                else:
                    conviction = 0.0
                
                if conviction >= self.min_range_threshold:
                    if close_open_diff > 0:
                        self._first_30m_signal = 1  # bullish with conviction
                    else:
                        self._first_30m_signal = -1  # bearish with conviction
                # else: signal stays None (no conviction)
                return

        # Check if we're in the last 30 minutes of the session
        time_until_close = ctx.session_end - ctx.ts
        thirty_minutes_ns = 30 * 60 * 1_000_000_000
        
        in_last_30_minutes = time_until_close <= thirty_minutes_ns
        
        # Trade on a 5-minute bar close during the last 30 minutes
        # (not at the very last 30-minute bar, but earlier to allow development)
        if (
            in_last_30_minutes
            and self.fast_interval in ctx.bars
            and self._first_30m_signal is not None
            and not self._traded_this_session
            and ctx.position == 0
            and not ctx.working_order_ids
        ):
            # Adapt bracket offsets based on first-bar volatility
            # Higher volatility in first 30m → wider brackets
            volatility_mult = 1.0
            if self._first_30m_range > 0:
                # Scale bracket size with observed volatility
                volatility_mult = max(0.8, min(1.5, self._first_30m_range / 1.0))
            
            adjusted_stop = self.stop_offset * volatility_mult
            adjusted_target = self.target_offset * volatility_mult
            
            if self._first_30m_signal == 1:
                # First 30m was bullish, go long
                ctx.buy(
                    size=self.size,
                    stop_offset=adjusted_stop,
                    target_offset=adjusted_target,
                    tag="first-30m-bullish",
                )
            else:  # self._first_30m_signal == -1
                # First 30m was bearish, go short
                ctx.sell(
                    size=self.size,
                    stop_offset=adjusted_stop,
                    target_offset=adjusted_target,
                    tag="first-30m-bearish",
                )
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        """Track fills."""
        pass

    def on_lifecycle(self, ctx) -> None:
        """Handle order lifecycle events."""
        pass
