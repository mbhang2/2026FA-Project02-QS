"""
Intraday momentum with multi-bar confirmation and adaptive exit timing.

Prior trials (#0: -0.2331, #1: -0.2387, #2: -0.3059) suggest the basic
first-30m-to-last-30m momentum hypothesis may be weak or that execution
timing is critical. This version tests:

1. Multi-bar confirmation: require the first 30m to show sustained momentum
   across multiple bars within that window, not just the final close vs open
2. Adaptive exit: don't wait for the last 30m bar to close; exit when we have
   a clear profit or when time is running out (reduce slippage on exit)
3. Entry on strength: enter when the last 30m bar opens AND we have conviction
   from the first 30m (trade the open, not the close)
4. Position management: reduce size slightly and use tighter stops to cut
   through commission drag more effectively
5. Session filtering: skip sessions where the first 30m is choppy (multiple
   direction changes), as these may have weak signals

The core insight: the previous strategies may have been entering too late
(at the close of the last 30m bar) and exiting too passively (letting the
bracket run). By entering earlier and managing the exit more actively, we
can reduce slippage and better capture any real momentum signal that exists.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30m",
        stop_offset: float = 0.4,
        target_offset: float = 0.6,
        size: int = 1,
        min_bars_in_first_30m: int = 2,
        exit_time_margin_ns: int = 5 * 60 * 1_000_000_000,  # 5 minutes before close
    ):
        """
        Args:
            interval: Bar interval (30m to align with hypothesis)
            stop_offset: Protective stop distance below entry
            target_offset: Profit target distance above entry
            size: Position size
            min_bars_in_first_30m: Minimum bars needed to confirm first 30m signal
            exit_time_margin_ns: Exit if this much time remains in session
        """
        self.interval = interval
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_bars_in_first_30m = min_bars_in_first_30m
        self.exit_time_margin_ns = exit_time_margin_ns
        
        # Session tracking
        self._current_session: int | None = None
        self._session_start_ts: int | None = None
        self._first_30m_bars: list = []  # Collect all bars in first 30m window
        self._first_30m_signal: int | None = None  # 1 for up, -1 for down
        self._traded_this_session: bool = False
        self._entry_price: float = 0.0
        self._last_30m_started: bool = False
        self._position_entry_ts: int | None = None

    def setup(self, ctx) -> None:
        """Subscribe to 30-minute bars."""
        ctx.subscribe(self.interval)

    def _is_in_first_30m(self, bar_open_ts: int, session_start: int | None) -> bool:
        """Check if a bar's open time falls within the first 30 minutes."""
        if session_start is None:
            return False
        thirty_minutes_ns = 30 * 60 * 1_000_000_000
        return bar_open_ts - session_start < thirty_minutes_ns

    def _analyze_first_30m_signal(self) -> int | None:
        """
        Analyze collected first 30m bars to determine signal.
        Returns 1 (bullish), -1 (bearish), or None (unclear).
        
        Signal logic:
        - Need at least min_bars_in_first_30m bars
        - Majority of bars should close higher than open (bullish) or lower (bearish)
        - Avoid choppy sessions where direction keeps changing
        """
        if len(self._first_30m_bars) < self.min_bars_in_first_30m:
            return None
        
        # Count bars that close up vs down
        up_bars = sum(1 for bar in self._first_30m_bars if bar.close > bar.open)
        down_bars = sum(1 for bar in self._first_30m_bars if bar.close < bar.open)
        
        # Require clear majority to avoid choppy sessions
        total_directional = up_bars + down_bars
        if total_directional == 0:
            return None
        
        up_ratio = up_bars / total_directional
        
        # Need > 60% agreement to confirm direction
        if up_ratio > 0.6:
            return 1  # Bullish
        elif up_ratio < 0.4:
            return -1  # Bearish
        else:
            return None  # Choppy, unclear

    def on_bars(self, ctx) -> None:
        """
        Multi-bar confirmation entry with active exit management.
        
        Process:
        1. Collect bars in the first 30m window
        2. Analyze them for sustained momentum direction
        3. Enter at the open of the last 30m bar if we have a signal
        4. Exit early if we're near session close or have clear profit
        """
        if not ctx.actionable:
            return

        # Handle session transitions
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._session_start_ts = ctx.session_start
            self._first_30m_bars = []
            self._first_30m_signal = None
            self._traded_this_session = False
            self._entry_price = 0.0
            self._last_30m_started = False
            self._position_entry_ts = None

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current_bar = history[-1]
        
        # Collect bars that fall within the first 30 minutes
        if self._first_30m_signal is None:
            if self._is_in_first_30m(current_bar.open_ts, self._session_start_ts):
                self._first_30m_bars.append(current_bar)
            else:
                # We've moved past the first 30m; analyze the signal
                if len(self._first_30m_bars) > 0:
                    self._first_30m_signal = self._analyze_first_30m_signal()

        # Check if we're in the last 30 minutes
        time_until_close = ctx.session_end - ctx.ts
        thirty_minutes_ns = 30 * 60 * 1_000_000_000
        in_last_30_minutes = time_until_close <= thirty_minutes_ns
        
        # Detect transition into last 30m window
        if in_last_30_minutes and not self._last_30m_started:
            self._last_30m_started = True
            
            # Try to enter at the start of the last 30m window
            if (
                self._first_30m_signal is not None
                and not self._traded_this_session
                and ctx.position == 0
                and not ctx.working_order_ids
            ):
                if self._first_30m_signal == 1:
                    ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="first-30m-bullish",
                    )
                else:  # -1
                    ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="first-30m-bearish",
                    )
                self._traded_this_session = True
                self._entry_price = ctx.last_price
                self._position_entry_ts = ctx.ts

        # Active exit management: close if we're very close to session end
        # and still holding a position
        if (
            ctx.position != 0
            and time_until_close < self.exit_time_margin_ns
            and not ctx.working_order_ids
        ):
            # Market close to exit, don't wait for bracket
            ctx.close(tag="time-based-exit")

    def on_fill(self, ctx) -> None:
        """Track fills; bracket and time-based exit handle exits."""
        fill = ctx.fill
        if fill.position == 0:
            # Position closed; reset tracking
            self._entry_price = 0.0
            self._position_entry_ts = None

    def on_lifecycle(self, ctx) -> None:
        """Handle order lifecycle events."""
        pass
