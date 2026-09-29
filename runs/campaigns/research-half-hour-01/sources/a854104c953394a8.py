"""
Intraday momentum: testing extended holding period with asymmetric exits.

All prior trials show persistent negative returns (-0.13 to -0.35), with the
best result (#4) at -0.1322 achieved by:
1. Volatility filtering (only trade in low-vol periods)
2. Early profit-taking (exit quickly on small wins)
3. Aggressive position limits (max 2 bars held)

Key observation: Trial #4 reduced losses by exiting FAST. But what if we're
exiting TOO fast and missing the actual signal? The hypothesis was that the
first 30m predicts the last 30m, but we've been testing it with exits that
happen before the last 30m window even closes.

This version tests a different thesis:
1. The first 30m signal IS valid, but requires holding through the full
   last 30m window to capture the move (not exiting early)
2. Asymmetric risk: use tight stops (cut losses fast) but let winners run
   until the last 30m bar closes
3. Only trade in the most favorable volatility regime (bottom 20%)
4. Reduce position size to 1 contract (minimize commission drag)
5. Use the bracket mechanism properly: let it manage the exit, don't override

The key change from #4: instead of early profit-taking at +0.3 points,
let the bracket handle the exit at +0.5-0.8 points, but keep the stop
tight at 0.3-0.4 points. This creates an asymmetric risk/reward where
we cut losses quickly but capture the full move if it comes.

Also: only enter trades when we're truly in low volatility (percentile < 0.2),
not 0.4. This is more selective and should reduce false signals.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30m",
        size: int = 1,
        vol_lookback: int = 5,
        vol_percentile_threshold: float = 0.2,
        stop_offset: float = 0.35,
        target_offset: float = 0.65,
    ):
        """
        Args:
            interval: Bar interval (30m for first/last 30m hypothesis)
            size: Position size (1 contract to minimize commission)
            vol_lookback: Bars to look back for volatility calculation
            vol_percentile_threshold: Only trade if vol is below this percentile
                                     (0.2 = only in bottom 20% least volatile periods)
            stop_offset: Stop loss distance (tight, cut losses fast)
            target_offset: Profit target distance (let winners run)
        """
        self.interval = interval
        self.size = size
        self.vol_lookback = vol_lookback
        self.vol_percentile_threshold = vol_percentile_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        # Session tracking
        self._current_session: int | None = None
        self._session_start_ts: int | None = None
        self._first_30m_bar: object | None = None
        self._first_30m_signal: int | None = None
        self._traded_this_session: bool = False
        
        # Volatility history for percentile calculation
        self._vol_history: list[float] = []

    def setup(self, ctx) -> None:
        """Subscribe to 30-minute bars."""
        ctx.subscribe(self.interval)

    def _calculate_volatility(self, bar) -> float:
        """Calculate volatility as (high - low) / open."""
        if bar.open <= 0:
            return 0.0
        return (bar.high - bar.low) / bar.open

    def _get_volatility_percentile(self) -> float:
        """
        Get the percentile of current volatility in history.
        Returns value in [0, 1] where 0 = lowest vol, 1 = highest vol.
        """
        if len(self._vol_history) < 2:
            return 0.5
        
        sorted_vols = sorted(self._vol_history)
        current_vol = self._vol_history[-1]
        
        # Count how many are below current
        below = sum(1 for v in sorted_vols if v <= current_vol)
        percentile = below / len(sorted_vols)
        return percentile

    def on_bars(self, ctx) -> None:
        """
        Trade first-30m momentum in ultra-low-volatility environments.
        Hold through last 30m window; let bracket manage the exit.
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
            self._vol_history = []

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current_bar = history[-1]
        
        # Track volatility for all bars
        current_vol = self._calculate_volatility(current_bar)
        self._vol_history.append(current_vol)
        # Keep only recent history
        if len(self._vol_history) > max(self.vol_lookback * 4, 25):
            self._vol_history.pop(0)
        
        # Capture the first 30-minute bar
        if self._first_30m_bar is None:
            if current_bar.open_ts == self._session_start_ts:
                self._first_30m_bar = current_bar
                
                # Generate signal: direction only
                if current_bar.close > current_bar.open:
                    self._first_30m_signal = 1  # bullish
                elif current_bar.close < current_bar.open:
                    self._first_30m_signal = -1  # bearish
                return

        # Check if we're in the last 30 minutes
        time_until_close = ctx.session_end - ctx.ts
        thirty_minutes_ns = 30 * 60 * 1_000_000_000
        in_last_30_minutes = time_until_close <= thirty_minutes_ns
        
        # Entry logic: trade on last 30m bar if conditions met
        if (
            in_last_30_minutes
            and self._first_30m_signal is not None
            and not self._traded_this_session
            and ctx.position == 0
            and not ctx.working_order_ids
        ):
            # Ultra-high-conviction filter: only trade in bottom 20% volatility
            vol_percentile = self._get_volatility_percentile()
            
            if vol_percentile <= self.vol_percentile_threshold:
                # Same-direction momentum hypothesis
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

    def on_fill(self, ctx) -> None:
        """Track fills; bracket handles exits."""
        pass

    def on_lifecycle(self, ctx) -> None:
        """Handle order lifecycle events."""
        pass
