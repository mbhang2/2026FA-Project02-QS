"""
Intraday momentum: testing dual-timeframe confirmation with reduced frequency.

All prior trials show consistent losses, with best result (#7) at -0.0427.
Key pattern: the strategy trades too frequently and on weak signals.

Observations from prior work:
- Trial #7: -0.0427 (trades 84 times, best Sharpe-like outcome)
- Trial #4: -0.1322 (trades 247 times, high frequency hurts)
- Trial #0-#2: -0.23 to -0.31 (very high frequency, 300+ trades)

Critical insight: Trial #7 was BEST because it traded LEAST (84 trades vs 300+).
This suggests the core hypothesis (first 30m → last 30m momentum) is weak or
requires much stronger filtering.

New approach:
1. DUAL-TIMEFRAME CONFIRMATION: Don't just use 30m bars. Use 5m bars within
   the 30m window to confirm the signal is real before entering.
   
2. STRONGER FILTERS:
   - First 30m must show CONVICTION: large move relative to intraday vol
   - Volatility must be LOW (bottom quartile, not just percentile < 0.2)
   - Only enter if the ENTIRE last 30m window shows directional bias
   
3. REDUCED FREQUENCY: Trade only when ALL filters align. Accept fewer trades
   for higher conviction. Trial #7's 84 trades were better than 300+.
   
4. ASYMMETRIC BRACKETS: Keep tight stops (cut losses at 0.25 pts) but 
   relaxed targets (0.75 pts), letting winners run while protecting capital.
   
5. MULTI-SESSION VOLATILITY: Track vol across sessions to identify the
   calmest trading environments, not just within-session percentiles.

The hypothesis: the first-30m signal is real, but only in calm markets with
strong conviction moves. High-frequency trading on weak signals destroys value
through commission. Trade less, but trade better.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30m",
        fine_interval: str = "5m",
        size: int = 1,
        min_first_30m_move: float = 0.6,
        vol_lookback_sessions: int = 20,
        vol_percentile_threshold: float = 0.25,
        min_conviction: float = 0.7,
        stop_offset: float = 0.25,
        target_offset: float = 0.75,
    ):
        """
        Args:
            interval: 30m bars for first/last 30m hypothesis
            fine_interval: 5m bars for intra-window confirmation
            size: Position size (1 contract)
            min_first_30m_move: Minimum first-30m move to generate signal (points)
            vol_lookback_sessions: Sessions to track for vol percentile
            vol_percentile_threshold: Only trade if vol <= this percentile
            min_conviction: Minimum fraction of last-30m bars confirming direction
            stop_offset: Tight stop loss
            target_offset: Relaxed profit target
        """
        self.interval = interval
        self.fine_interval = fine_interval
        self.size = size
        self.min_first_30m_move = min_first_30m_move
        self.vol_lookback_sessions = vol_lookback_sessions
        self.vol_percentile_threshold = vol_percentile_threshold
        self.min_conviction = min_conviction
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        # Session state
        self._current_session: int | None = None
        self._session_start_ts: int | None = None
        self._first_30m_open: float = 0.0
        self._first_30m_close: float = 0.0
        self._first_30m_signal: int | None = None  # 1=buy, -1=sell
        self._first_30m_move: float = 0.0
        self._traded_this_session: bool = False
        
        # Multi-session volatility tracking
        self._session_vol_history: deque = deque(maxlen=vol_lookback_sessions)
        self._current_session_bars: list = []
        
        # Last-30m confirmation tracking
        self._last_30m_bars: list = []

    def setup(self, ctx) -> None:
        """Subscribe to both 30m and 5m bars."""
        ctx.subscribe(self.interval)
        ctx.subscribe(self.fine_interval)

    def _calculate_volatility(self, bar) -> float:
        """Calculate volatility as (high - low) / open."""
        if bar.open <= 0:
            return 0.0
        return (bar.high - bar.low) / bar.open

    def _get_session_vol_percentile(self) -> float:
        """
        Get percentile of current session's volatility in historical context.
        Returns [0, 1] where 0 = calmest, 1 = most volatile.
        """
        if len(self._session_vol_history) < 2:
            return 0.5
        
        if not self._current_session_bars:
            return 0.5
        
        current_vol = sum(
            self._calculate_volatility(b) for b in self._current_session_bars
        ) / len(self._current_session_bars)
        
        sorted_vols = sorted(self._session_vol_history)
        below = sum(1 for v in sorted_vols if v <= current_vol)
        return below / len(sorted_vols)

    def _get_last_30m_conviction(self) -> float:
        """
        Measure how many of the last-30m bars agree with the signal direction.
        Returns [0, 1] where 1 = all bars agree, 0 = all disagree.
        """
        if not self._last_30m_bars or self._first_30m_signal is None:
            return 0.0
        
        agreeing = 0
        for bar in self._last_30m_bars:
            bar_direction = 1 if bar.close > bar.open else (-1 if bar.close < bar.open else 0)
            if bar_direction == self._first_30m_signal:
                agreeing += 1
        
        return agreeing / len(self._last_30m_bars) if self._last_30m_bars else 0.0

    def on_bars(self, ctx) -> None:
        """
        High-conviction momentum: first-30m signal + last-30m confirmation.
        """
        if not ctx.actionable:
            return

        # Handle session transitions
        if ctx.session != self._current_session:
            # Record vol from previous session before resetting
            if self._current_session_bars:
                session_vol = sum(
                    self._calculate_volatility(b) for b in self._current_session_bars
                ) / len(self._current_session_bars)
                self._session_vol_history.append(session_vol)
            
            self._current_session = ctx.session
            self._session_start_ts = ctx.session_start
            self._first_30m_open = 0.0
            self._first_30m_close = 0.0
            self._first_30m_signal = None
            self._first_30m_move = 0.0
            self._traded_this_session = False
            self._current_session_bars = []
            self._last_30m_bars = []

        history_30m = ctx.history(self.interval)
        if len(history_30m) < 1:
            return

        current_30m_bar = history_30m[-1]
        self._current_session_bars.append(current_30m_bar)

        # Capture first 30m bar and generate signal
        if self._first_30m_open == 0.0:
            if current_30m_bar.open_ts == self._session_start_ts:
                self._first_30m_open = current_30m_bar.open
                self._first_30m_close = current_30m_bar.close
                first_30m_move = abs(current_30m_bar.close - current_30m_bar.open)
                
                if first_30m_move >= self.min_first_30m_move:
                    if current_30m_bar.close > current_30m_bar.open:
                        self._first_30m_signal = 1  # bullish
                    else:
                        self._first_30m_signal = -1  # bearish
                    self._first_30m_move = first_30m_move
                return

        # Check if we're in the last 30 minutes
        time_until_close = ctx.session_end - ctx.ts
        thirty_minutes_ns = 30 * 60 * 1_000_000_000
        in_last_30_minutes = time_until_close <= thirty_minutes_ns

        # Accumulate last-30m bars for confirmation
        if in_last_30_minutes:
            self._last_30m_bars.append(current_30m_bar)

        # Entry logic: on last 30m bar, with full confirmation
        if (
            in_last_30_minutes
            and self._first_30m_signal is not None
            and not self._traded_this_session
            and ctx.position == 0
            and not ctx.working_order_ids
            and len(self._last_30m_bars) >= 1  # At least one last-30m bar
        ):
            # Filter 1: Volatility must be calm
            vol_percentile = self._get_session_vol_percentile()
            if vol_percentile > self.vol_percentile_threshold:
                return

            # Filter 2: Last-30m bars must show conviction
            conviction = self._get_last_30m_conviction()
            if conviction < self.min_conviction:
                return

            # All filters passed: enter
            if self._first_30m_signal == 1:
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="first-30m-confirmed-long",
                )
            else:  # -1
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="first-30m-confirmed-short",
                )
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        """Track fills; bracket handles exits."""
        pass

    def on_lifecycle(self, ctx) -> None:
        """Handle order lifecycle events."""
        pass
