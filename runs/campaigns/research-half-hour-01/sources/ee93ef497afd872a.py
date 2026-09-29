"""
Intraday momentum with refined signal detection and reduced friction.

Prior trials scored -0.2331 and -0.2387, suggesting the basic hypothesis
may be weak or that execution timing/costs are dominating. This version tests:

1. Stricter signal confirmation: require the first 30m to close beyond the
   session open price by a meaningful amount (not just close > open)
2. Entry timing: enter at the *close* of the last 30m (more certain signal)
   rather than at the open (speculation about direction)
3. Reduced position size to lower commission drag per trade
4. Tighter brackets to cut losses faster on false signals
5. Only trade sessions where first 30m has clear directional conviction

The insight: momentum strategies in liquid, efficient markets like NQ futures
are fighting transaction costs. Stricter signal thresholds and tighter exits
may improve the signal-to-noise ratio relative to friction.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30m",
        stop_offset: float = 0.35,
        target_offset: float = 0.5,
        size: int = 1,
        signal_threshold: float = 0.15,
    ):
        """
        Args:
            interval: Bar interval for 30-minute windows
            stop_offset: Distance below entry for protective stop
            target_offset: Distance above entry for profit target
            size: Position size per trade
            signal_threshold: Minimum move from open as % of first 30m range
                            to confirm direction (0.15 = 15% of range)
        """
        self.interval = interval
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.signal_threshold = signal_threshold
        
        # Session tracking
        self._current_session: int | None = None
        self._session_start_ts: int | None = None
        self._first_30m_bar: object | None = None
        self._first_30m_signal: int | None = None
        self._first_30m_open: float = 0.0
        self._first_30m_range: float = 0.0
        self._traded_this_session: bool = False
        self._in_last_30m_window: bool = False

    def setup(self, ctx) -> None:
        """Subscribe to 30-minute bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Trade based on first 30m direction, entering at last 30m close.
        
        Key changes:
        1. Stricter signal: first 30m must close far enough from open
           (by signal_threshold % of its range) to confirm direction
        2. Enter at the close of the last 30-minute bar (more certain)
        3. Reduce position size and tighten stops to reduce friction
        4. Only trade sessions with strong first-30m conviction
        """
        if not ctx.actionable:
            return

        # Handle session transitions
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._session_start_ts = ctx.session_start
            self._first_30m_bar = None
            self._first_30m_signal = None
            self._first_30m_open = 0.0
            self._first_30m_range = 0.0
            self._traded_this_session = False
            self._in_last_30m_window = False

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current_bar = history[-1]
        
        # Capture the first 30-minute bar of the session
        if self._first_30m_bar is None:
            if current_bar.open_ts == self._session_start_ts:
                self._first_30m_bar = current_bar
                self._first_30m_open = current_bar.open
                self._first_30m_range = current_bar.high - current_bar.low
                
                # Generate signal only if range is meaningful
                if self._first_30m_range > 0:
                    # How far did it move from open as % of range?
                    move_from_open = current_bar.close - current_bar.open
                    move_pct = move_from_open / self._first_30m_range
                    
                    # Require conviction: move must be > signal_threshold % of range
                    if move_pct > self.signal_threshold:
                        self._first_30m_signal = 1  # bullish
                    elif move_pct < -self.signal_threshold:
                        self._first_30m_signal = -1  # bearish
                    # else: signal stays None (weak or no direction)
                return

        # Check if we're in the last 30 minutes of the session
        time_until_close = ctx.session_end - ctx.ts
        thirty_minutes_ns = 30 * 60 * 1_000_000_000
        
        in_last_30_minutes = time_until_close <= thirty_minutes_ns
        
        # Detect entry into the last 30-minute window
        if in_last_30_minutes and not self._in_last_30m_window:
            self._in_last_30m_window = True
        
        # Trade on the close of the last 30-minute bar
        # (when we're in the last 30m window and this bar just closed)
        if (
            self._in_last_30m_window
            and self._first_30m_signal is not None
            and not self._traded_this_session
            and ctx.position == 0
            and not ctx.working_order_ids
        ):
            # Only trade if we're actually in the last 30m still
            # (avoid trading after the session has closed)
            if in_last_30_minutes:
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
        """Track fills; bracket handles exits."""
        pass

    def on_lifecycle(self, ctx) -> None:
        """Handle order lifecycle events."""
        pass
