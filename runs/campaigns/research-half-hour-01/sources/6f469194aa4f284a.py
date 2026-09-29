"""
Intraday momentum with volatility-adjusted entry and early exit optimization.

Prior trials show persistent negative returns (-0.23 to -0.31), suggesting:
1. The basic first-30m-to-last-30m momentum hypothesis may be too weak
2. Commission (0.1 index points per side) is a major drag on frequent trades
3. Entry/exit timing and slippage are critical friction sources

This version tests a different approach:
1. Use volatility to size position and adjust bracket levels dynamically
2. Enter only when the first 30m shows BOTH direction AND low volatility
   (more confident signal, less noise)
3. Exit much earlier than the last 30m bar close - take profits quickly
   when available, don't hold through session close
4. Reduce trade frequency by trading only high-conviction setups
5. Use tighter stops to cut losses faster on false signals

Key insight: The negative returns suggest we're fighting both a weak signal
and heavy transaction costs. Instead of trying to extract more from the
momentum hypothesis, reduce costs by:
- Trading fewer times (higher conviction filter)
- Exiting faster (don't hold to session close)
- Sizing based on volatility (risk management)
- Using volatility-adjusted stops (don't overshoot on noise)
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30m",
        size: int = 1,
        vol_lookback: int = 5,
        vol_percentile_threshold: float = 0.4,
        max_stop_offset: float = 0.5,
        max_target_offset: float = 0.8,
        early_exit_profit_threshold: float = 0.3,
        hold_bars_max: int = 2,
    ):
        """
        Args:
            interval: Bar interval (30m)
            size: Base position size
            vol_lookback: Bars to look back for volatility calculation
            vol_percentile_threshold: Only trade if vol is below this percentile
                                     (0.4 = only trade in 40% least volatile periods)
            max_stop_offset: Maximum stop distance (adjusted by volatility)
            max_target_offset: Maximum target distance (adjusted by volatility)
            early_exit_profit_threshold: Exit if profit reaches this (in points)
            hold_bars_max: Maximum bars to hold position (aggressive exit)
        """
        self.interval = interval
        self.size = size
        self.vol_lookback = vol_lookback
        self.vol_percentile_threshold = vol_percentile_threshold
        self.max_stop_offset = max_stop_offset
        self.max_target_offset = max_target_offset
        self.early_exit_profit_threshold = early_exit_profit_threshold
        self.hold_bars_max = hold_bars_max
        
        # Session tracking
        self._current_session: int | None = None
        self._session_start_ts: int | None = None
        self._first_30m_bar: object | None = None
        self._first_30m_signal: int | None = None
        self._first_30m_vol: float = 0.0
        self._traded_this_session: bool = False
        self._bars_held: int = 0
        self._entry_price: float = 0.0
        
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
        Trade first-30m signal only in low-volatility environments.
        Exit early when profit target is hit or time limit reached.
        """
        if not ctx.actionable:
            return

        # Handle session transitions
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._session_start_ts = ctx.session_start
            self._first_30m_bar = None
            self._first_30m_signal = None
            self._first_30m_vol = 0.0
            self._traded_this_session = False
            self._bars_held = 0
            self._entry_price = 0.0
            self._vol_history = []

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current_bar = history[-1]
        
        # Track volatility for all bars
        current_vol = self._calculate_volatility(current_bar)
        self._vol_history.append(current_vol)
        # Keep only recent history
        if len(self._vol_history) > max(self.vol_lookback * 3, 20):
            self._vol_history.pop(0)
        
        # Capture the first 30-minute bar
        if self._first_30m_bar is None:
            if current_bar.open_ts == self._session_start_ts:
                self._first_30m_bar = current_bar
                self._first_30m_vol = current_vol
                
                # Generate signal: direction
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
            # High-conviction filter: only trade if first 30m vol was low
            # (signal is cleaner in low-vol environment)
            vol_percentile = self._get_volatility_percentile()
            
            if vol_percentile <= self.vol_percentile_threshold:
                # Adjust bracket sizes based on volatility
                # Lower vol = tighter stops (more confident)
                # Higher vol = wider stops (less noise)
                vol_adjustment = 1.0 + (vol_percentile * 0.5)  # 1.0 to 1.5 multiplier
                
                stop_offset = self.max_stop_offset / vol_adjustment
                target_offset = self.max_target_offset / vol_adjustment
                
                if self._first_30m_signal == 1:
                    ctx.buy(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="first-30m-bullish",
                    )
                else:  # -1
                    ctx.sell(
                        size=self.size,
                        stop_offset=stop_offset,
                        target_offset=target_offset,
                        tag="first-30m-bearish",
                    )
                self._traded_this_session = True
                self._entry_price = ctx.last_price
                self._bars_held = 0

        # Active position management
        if ctx.position != 0:
            self._bars_held += 1
            
            # Early exit: close if profit threshold reached
            if self._entry_price > 0:
                current_pnl = abs(ctx.last_price - self._entry_price) * ctx.position
                if current_pnl >= self.early_exit_profit_threshold and ctx.position != 0:
                    # Take the profit, don't wait for bracket
                    ctx.close(tag="early-exit-profit")
                    self._bars_held = 0
                    self._entry_price = 0.0
            
            # Hard exit: close if held too long
            if self._bars_held >= self.hold_bars_max and ctx.position != 0:
                ctx.close(tag="time-limit-exit")
                self._bars_held = 0
                self._entry_price = 0.0

    def on_fill(self, ctx) -> None:
        """Track fills; bracket handles most exits."""
        fill = ctx.fill
        if fill.position == 0:
            self._entry_price = 0.0
            self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        """Handle order lifecycle events."""
        pass
