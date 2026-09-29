"""
Intraday momentum: testing reverse hypothesis with counter-trend filtering.

All trials show persistent losses (-0.04 to -0.35), suggesting the core
hypothesis (first 30m predicts last 30m momentum) may be WRONG or inverted.

Key observations from prior work:
- Trial #7: -0.0427 (84 trades, least frequent = least bad)
- Trial #8: -0.1169 (65 trades, added confirmation but still negative)
- Pattern: ALL trials lose money consistently, suggesting systematic bias

Critical insight: What if the effect is REVERSED? What if the first 30m
predicts a REVERSAL in the last 30m, not a continuation? This would explain:
1. Why every continuation-based strategy loses money
2. Why reducing trade frequency helps slightly (fewer bad trades)
3. Why adding "conviction" filters don't help (we're trading the wrong direction)

New approach - TEST THE REVERSAL HYPOTHESIS:
1. FLIP THE SIGNAL: Trade opposite to first-30m direction
2. MEAN REVERSION LOGIC: If first 30m is up, expect mean reversion down
3. AGGRESSIVE FILTERING: Only trade when first 30m move is LARGE (conviction
   that a reversal is coming), but in the opposite direction
4. TIGHTER BRACKETS: Use very tight stops (0.2 pts) and modest targets (0.4 pts)
   to capture quick mean-reversion moves
5. MINIMAL TRADING: Only enter when first-30m move is extreme (top 25% of moves)
   to maximize signal-to-noise ratio

The logic: Extreme first-30m moves (up or down) often overextend, and the
last 30m corrects. This is a classic mean-reversion pattern. By trading the
opposite of the first 30m in calm markets with large first-30m moves, we're
betting on reversion to the mean within the same session.

This is a CRITICAL TEST of whether the relationship is continuation or reversal.
If reversal works better than continuation, it invalidates the original
hypothesis and suggests a different market mechanism.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30m",
        size: int = 1,
        min_first_30m_move: float = 1.2,
        vol_lookback_sessions: int = 20,
        vol_percentile_threshold: float = 0.3,
        stop_offset: float = 0.2,
        target_offset: float = 0.4,
    ):
        """
        Args:
            interval: 30m bars for first/last 30m hypothesis
            size: Position size (1 contract)
            min_first_30m_move: Minimum first-30m move to generate signal (points)
                               Higher threshold = only trade extreme moves
            vol_lookback_sessions: Sessions to track for vol percentile
            vol_percentile_threshold: Only trade if vol <= this percentile
            stop_offset: Tight stop loss (mean reversion exits quickly on failure)
            target_offset: Modest profit target (capture quick reversions)
        """
        self.interval = interval
        self.size = size
        self.min_first_30m_move = min_first_30m_move
        self.vol_lookback_sessions = vol_lookback_sessions
        self.vol_percentile_threshold = vol_percentile_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        # Session state
        self._current_session: int | None = None
        self._session_start_ts: int | None = None
        self._first_30m_open: float = 0.0
        self._first_30m_close: float = 0.0
        self._first_30m_signal: int | None = None  # 1=reversal up, -1=reversal down
        self._first_30m_move: float = 0.0
        self._traded_this_session: bool = False
        
        # Multi-session volatility tracking
        self._session_vol_history: deque = deque(maxlen=vol_lookback_sessions)
        self._current_session_bars: list = []
        
        # Track first-30m move statistics for percentile calculation
        self._first_30m_moves: deque = deque(maxlen=vol_lookback_sessions * 2)

    def setup(self, ctx) -> None:
        """Subscribe to 30m bars."""
        ctx.subscribe(self.interval)

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

    def _is_extreme_first_30m_move(self) -> bool:
        """
        Check if first-30m move is in top quartile of historical moves.
        """
        if not self._first_30m_moves:
            return False
        
        sorted_moves = sorted(self._first_30m_moves)
        threshold_idx = max(0, int(len(sorted_moves) * 0.75))
        threshold = sorted_moves[threshold_idx] if sorted_moves else self.min_first_30m_move
        
        return self._first_30m_move >= max(threshold, self.min_first_30m_move)

    def on_bars(self, ctx) -> None:
        """
        Mean reversion: trade OPPOSITE to first-30m direction.
        Only when first 30m is extreme and session is calm.
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

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current_bar = history[-1]
        self._current_session_bars.append(current_bar)

        # Capture first 30m bar and generate REVERSAL signal
        if self._first_30m_open == 0.0:
            if current_bar.open_ts == self._session_start_ts:
                self._first_30m_open = current_bar.open
                self._first_30m_close = current_bar.close
                self._first_30m_move = abs(current_bar.close - current_bar.open)
                
                # Track move for percentile calculation
                self._first_30m_moves.append(self._first_30m_move)
                
                # REVERSAL HYPOTHESIS: trade opposite direction
                if current_bar.close > current_bar.open:
                    self._first_30m_signal = -1  # first 30m up -> expect down
                elif current_bar.close < current_bar.open:
                    self._first_30m_signal = 1   # first 30m down -> expect up
                return

        # Check if we're in the last 30 minutes
        time_until_close = ctx.session_end - ctx.ts
        thirty_minutes_ns = 30 * 60 * 1_000_000_000
        in_last_30_minutes = time_until_close <= thirty_minutes_ns

        # Entry logic: on last 30m bar, with extreme move + calm vol
        if (
            in_last_30_minutes
            and self._first_30m_signal is not None
            and not self._traded_this_session
            and ctx.position == 0
            and not ctx.working_order_ids
        ):
            # Filter 1: First-30m move must be extreme (top quartile)
            if not self._is_extreme_first_30m_move():
                return

            # Filter 2: Volatility must be calm (bottom 30%)
            vol_percentile = self._get_session_vol_percentile()
            if vol_percentile > self.vol_percentile_threshold:
                return

            # All filters passed: enter OPPOSITE to first-30m direction
            if self._first_30m_signal == 1:  # expect up (first 30m was down)
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="reversal-long",
                )
            else:  # -1, expect down (first 30m was up)
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="reversal-short",
                )
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        """Track fills; bracket handles exits."""
        pass

    def on_lifecycle(self, ctx) -> None:
        """Handle order lifecycle events."""
        pass
