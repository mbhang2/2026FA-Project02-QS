"""
Flow continuation strategy: refined micro-confirmation with tighter thresholds.

Analysis of recent trials:
- #21: -0.2980, 316 trades (0.82/session) → 1m micro-confirmation hurt performance
- #20: -0.1665, 361 trades (0.94/session) → asymmetric thresholds + volume norm helped
- #19: -0.1631, 382 trades (0.99/session) → multi-timeframe confirmation working
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best so far

Critical insight:
#21 added 1m micro-confirmation and DROPPED to -0.2980. This is a major regression.
The 1m bars are too noisy and are filtering out too many valid signals.

Going back to #19's approach (5m + 15m only) but with refinements:
1. #19 had 382 trades at -0.1631. This is better than #20's 361 trades at -0.1665.
2. The multi-timeframe confirmation (5m signal + 15m confirmation) is working.
3. We need to improve the edge per trade without adding noise.

New hypothesis - TIGHTER THRESHOLDS WITH STRICT CONFIRMATION:
Instead of asymmetric thresholds that make buy easier than sell, use SYMMETRIC but
STRICTER thresholds:
- Raise both to 0.57 (higher conviction)
- Require 15m confirmation at 0.58 (very strong agreement)
- This filters to only the highest-conviction signals
- Fewer trades but better edge per trade

Why this might work:
- #9 (best at -0.1437) had 768 trades at 0.55 threshold
- #19 (good at -0.1631) had 382 trades with multi-timeframe
- The relationship: fewer, higher-conviction trades are better
- We're at the point where QUALITY >> QUANTITY
- Stricter thresholds = higher win rate, even if lower frequency

The logic:
- 5m bar must show 57%+ on one side (higher conviction than 0.55)
- Previous bar must not be strongly opposed (0.55 threshold)
- 15m confirmation must show 58%+ on same side (very strong agreement)
- Price action: close in upper/lower 60% (same as before)
- Volume: above session average (keeps it working)
- Only 1 entry per session (proven better)
- Keep 5m + 15m (drop 1m which hurt)

Expected outcome:
- 250-350 trades (fewer than #19's 382)
- Better edge per trade from stricter entry rules
- Score potentially in -0.14 to -0.16 range

The core issue we're solving:
Commission is 0.20 per round turn. At -0.1437 with 768 trades, we're making
~0.17 per trade gross. We need to get to 0.20+ per trade to be profitable.
Stricter thresholds should improve win rate and edge per trade.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "5m",
        slow_interval: str = "15m",
        fast_threshold: float = 0.57,
        slow_confirmation_threshold: float = 0.58,
        prev_threshold: float = 0.55,
        close_threshold: float = 0.60,
        stop_offset: float = 1.5,
        target_offset: float = 3.5,
        size: int = 1,
        max_entries_per_session: int = 1,
    ):
        """
        Args:
            fast_interval: Primary signal timeframe (5m per campaign spec)
            slow_interval: Confirmation timeframe (15m for strong agreement)
            fast_threshold: Fast bar requires >57% on one side (high conviction)
            slow_confirmation_threshold: Slow bar requires >58% (very strong agreement)
            prev_threshold: Previous bar rejection threshold (0.55)
            close_threshold: Close position in range threshold (0.60)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.5 points)
            size: Position size in contracts
            max_entries_per_session: Maximum entries per session
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.fast_threshold = fast_threshold
        self.slow_confirmation_threshold = slow_confirmation_threshold
        self.prev_threshold = prev_threshold
        self.close_threshold = close_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.max_entries_per_session = max_entries_per_session
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0
        
        # Track session volume statistics
        self._session_volumes: list[int] = []
        self._session_avg_volume: float = 200.0

    def setup(self, ctx) -> None:
        """Subscribe to 5m and 15m bars."""
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: strict symmetric thresholds with strong slow-interval confirmation.
        Enter only on highest-conviction signals: 5m at 57%+ AND 15m at 58%+.
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count (based on fast interval)
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
            self._session_volumes = []
            self._session_avg_volume = 200.0
        
        # Increment bar count only on fast interval
        if self.fast_interval in ctx.bars:
            self._bars_in_session += 1
        
        # One position at a time
        if ctx.position or ctx.working_order_ids:
            return
        
        # Limit entries per session
        if self._entries_this_session >= self.max_entries_per_session:
            return
        
        # Only process on fast interval closes
        if self.fast_interval not in ctx.bars:
            return
        
        fast_history = ctx.history(self.fast_interval)
        if len(fast_history) < 2:
            return
        
        slow_history = ctx.history(self.slow_interval)
        if len(slow_history) < 1:
            return
        
        last_fast = fast_history[-1]  # 5m bar that just closed
        last_slow = slow_history[-1]  # Most recent 15m bar
        
        # Update session volume statistics
        self._session_volumes.append(last_fast.volume)
        if len(self._session_volumes) > 0:
            self._session_avg_volume = sum(self._session_volumes) / len(self._session_volumes)
        
        # Skip first 3 bars of session (cash open microstructure is different)
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session (forced liquidation, end-of-day pressure)
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes (in nanoseconds)
            return
        
        # Volume filter: only trade bars above session average
        if last_fast.volume < self._session_avg_volume:
            return
        
        # Measure aggression on fast interval
        fast_buy_vol = last_fast.buy_volume
        fast_sell_vol = last_fast.sell_volume
        fast_total_vol = last_fast.volume
        
        if fast_total_vol <= 0:
            return
        
        fast_buy_ratio = fast_buy_vol / fast_total_vol
        fast_sell_ratio = fast_sell_vol / fast_total_vol
        
        # Measure aggression on slow interval
        slow_buy_vol = last_slow.buy_volume
        slow_sell_vol = last_slow.sell_volume
        slow_total_vol = last_slow.volume
        
        if slow_total_vol <= 0:
            return
        
        slow_buy_ratio = slow_buy_vol / slow_total_vol
        slow_sell_ratio = slow_sell_vol / slow_total_vol
        
        # Get previous fast bar for directional agreement
        prev_fast = fast_history[-2]
        if prev_fast.volume <= 0:
            return
        
        prev_buy_ratio = prev_fast.buy_volume / prev_fast.volume
        prev_sell_ratio = prev_fast.sell_volume / prev_fast.volume
        
        # BUY SIGNAL: Strict symmetric thresholds
        # Fast bar: >57% buy, Slow bar: >58% buy
        if fast_buy_ratio > self.fast_threshold:
            # Previous fast bar check: only reject if prev was strongly sell-dominated
            if prev_sell_ratio > self.prev_threshold:
                return
            
            # Slow interval confirmation: must show strong buying tendency (>58%)
            if slow_buy_ratio <= self.slow_confirmation_threshold:
                return
            
            # Price action confirmation on fast bar: close in upper 60%
            bar_range = last_fast.high - last_fast.low
            if bar_range > 0:
                close_position = (last_fast.close - last_fast.low) / bar_range
                if close_position < self.close_threshold:
                    return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # SELL SIGNAL: Strict symmetric thresholds
        # Fast bar: >57% sell, Slow bar: >58% sell
        elif fast_sell_ratio > self.fast_threshold:
            # Previous fast bar check: only reject if prev was strongly buy-dominated
            if prev_buy_ratio > self.prev_threshold:
                return
            
            # Slow interval confirmation: must show strong selling tendency (>58%)
            if slow_sell_ratio <= self.slow_confirmation_threshold:
                return
            
            # Price action confirmation on fast bar: close in lower 40%
            bar_range = last_fast.high - last_fast.low
            if bar_range > 0:
                close_position = (last_fast.close - last_fast.low) / bar_range
                if close_position > (1.0 - self.close_threshold):
                    return
            
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )
            self._entries_this_session += 1

    def on_fill(self, ctx) -> None:
        """
        Position is now open. The bracket (stop and target) was armed by the
        engine at fill time, protecting the position immediately.
        """
        pass

    def on_lifecycle(self, ctx) -> None:
        """
        Track lifecycle events. Bracket handles the exit.
        """
        pass
