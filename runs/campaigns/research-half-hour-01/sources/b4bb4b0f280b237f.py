"""
Intraday momentum strategy: first 30 minutes predicting last 30 minutes.

The hypothesis is that NQ's first-30-minute return direction predicts the 
last-30-minute return direction within the same session. This strategy:

1. Measures the first 30 minutes of the session (from session open)
2. Compares it to the last 30 minutes (ending at session close)
3. Enters on the close of the first 30-minute bar in the predicted direction
4. Holds until the close of the last 30-minute bar

The signal is: if first 30 min closes higher than it opened, expect last 30 min
to close higher than it opened (and vice versa for shorts).

Key refinements over the baseline:
- Uses 30-minute bars explicitly to match the hypothesis window
- Tracks session state to identify first and last 30-minute periods
- Only trades if we have enough history to identify both periods
- Uses session_end to know when the last 30 minutes begin
- Strict position management: one trade per session maximum
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30m",
        stop_offset: float = 0.75,
        target_offset: float = 0.75,
        size: int = 1,
    ):
        self.interval = interval
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        
        # Session tracking
        self._current_session: int | None = None
        self._session_start_ts: int | None = None
        self._first_30m_bar: object | None = None
        self._first_30m_signal: int | None = None  # 1 for up, -1 for down, None if unclear
        self._traded_this_session: bool = False
        self._entry_ts: int | None = None

    def setup(self, ctx) -> None:
        """Subscribe to 30-minute bars to match the hypothesis window."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Trade the first-30-minute signal at the last-30-minute close.
        
        Logic:
        1. On session change, reset tracking
        2. Capture the first 30-minute bar's direction
        3. When we enter the last 30 minutes, trade if we have a signal
        4. Exit on the close of the last 30-minute bar (via bracket)
        """
        if not ctx.actionable:
            return

        # Handle session transitions
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._session_start_ts = ctx.session_start
            self._first_30m_bar = None
            self._first_30m_signal = None
            self._traded_this_session = False
            self._entry_ts = None

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current_bar = history[-1]
        
        # Identify the first 30-minute bar of the session
        # It should be the first bar after session_start
        if self._first_30m_bar is None and len(history) >= 1:
            # The first bar in our history for this session
            if current_bar.open_ts == self._session_start_ts:
                self._first_30m_bar = current_bar
                # Determine signal: did first 30m close up or down?
                if current_bar.close > current_bar.open:
                    self._first_30m_signal = 1  # bullish
                elif current_bar.close < current_bar.open:
                    self._first_30m_signal = -1  # bearish
                # If open == close, signal stays None (no clear direction)
                return

        # Check if we're in the last 30 minutes of the session
        # Last 30 minutes means: session_end - 30 minutes <= current_ts < session_end
        time_until_close = ctx.session_end - ctx.ts
        thirty_minutes_ns = 30 * 60 * 1_000_000_000
        
        in_last_30_minutes = time_until_close <= thirty_minutes_ns
        
        # Trade on the last 30-minute bar close if we have a signal
        if (
            in_last_30_minutes
            and self._first_30m_signal is not None
            and not self._traded_this_session
            and ctx.position == 0
            and not ctx.working_order_ids
        ):
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
            self._entry_ts = ctx.ts

    def on_fill(self, ctx) -> None:
        """Track fills but let the bracket handle exits."""
        pass

    def on_lifecycle(self, ctx) -> None:
        """Handle order lifecycle events."""
        pass
