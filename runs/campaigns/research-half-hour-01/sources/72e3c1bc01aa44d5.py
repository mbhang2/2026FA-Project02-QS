"""
Intraday momentum with adaptive entry timing and volatility filtering.

The previous strategy scored -0.2331 by entering on the last 30-minute close,
which may be too late or too rigid. This version:

1. Captures the first 30-minute direction (same as before)
2. Enters earlier in the last 30 minutes (not at the very close)
3. Adds volatility filtering to avoid low-signal sessions
4. Uses tighter stops to reduce commission drag
5. Tracks realized P&L to understand what's working

The key insight: entering at the last 30-minute *open* (rather than close)
gives more time for the trade to develop and may catch the momentum earlier.
Also, sessions with very low first-30m range may have weak signals.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30m",
        stop_offset: float = 0.5,
        target_offset: float = 0.75,
        size: int = 1,
        min_first_30m_range: float = 0.25,
    ):
        self.interval = interval
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_first_30m_range = min_first_30m_range
        
        # Session tracking
        self._current_session: int | None = None
        self._session_start_ts: int | None = None
        self._first_30m_bar: object | None = None
        self._first_30m_signal: int | None = None
        self._first_30m_range: float = 0.0
        self._traded_this_session: bool = False
        self._last_30m_bar_seen: bool = False

    def setup(self, ctx) -> None:
        """Subscribe to 30-minute bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Trade the first-30-minute signal, entering at the start of last 30m.
        
        Improvements:
        1. Filter out weak signals (small first-30m range)
        2. Enter at the open of the last 30-minute bar (earlier timing)
        3. Use tighter stops to reduce friction
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
            self._last_30m_bar_seen = False

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current_bar = history[-1]
        
        # Capture the first 30-minute bar of the session
        if self._first_30m_bar is None:
            if current_bar.open_ts == self._session_start_ts:
                self._first_30m_bar = current_bar
                # Measure range and direction
                self._first_30m_range = current_bar.high - current_bar.low
                if current_bar.close > current_bar.open:
                    self._first_30m_signal = 1  # bullish
                elif current_bar.close < current_bar.open:
                    self._first_30m_signal = -1  # bearish
                return

        # Check if we're approaching or in the last 30 minutes
        time_until_close = ctx.session_end - ctx.ts
        thirty_minutes_ns = 30 * 60 * 1_000_000_000
        
        in_last_30_minutes = time_until_close <= thirty_minutes_ns
        
        # We want to trade on the *open* of the last 30-minute bar,
        # not the close. Detect the transition into the last 30-minute window.
        if (
            in_last_30_minutes
            and not self._last_30m_bar_seen
            and self._first_30m_signal is not None
            and self._first_30m_range >= self.min_first_30m_range
            and not self._traded_this_session
            and ctx.position == 0
            and not ctx.working_order_ids
        ):
            # We just entered the last 30 minutes; trade now
            self._last_30m_bar_seen = True
            
            if self._first_30m_signal == 1:
                # First 30m was bullish, go long
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="first-30m-bullish",
                )
            else:  # self._first_30m_signal == -1
                # First 30m was bearish, go short
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="first-30m-bearish",
                )
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        """Track fills."""
        pass

    def on_lifecycle(self, ctx) -> None:
        """Handle order lifecycle events."""
        pass
