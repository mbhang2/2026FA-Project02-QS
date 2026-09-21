"""
Flow continuation strategy: testing multi-timeframe confirmation with reduced entry rate.

Analysis of prior trials:
- #18: -0.1695, 384 trades (1.0/session) → reduced frequency helped but not enough
- #9/#10: -0.1437, 768 trades (2.0/session) → best so far
- #15: -0.1437, 768 trades (2.0/session) → tied best

Key insight from #18's modest improvement:
Reducing from 768 to 384 trades improved score from -0.1829 to -0.1695 (0.0134 improvement).
This confirms that entry selectivity matters more than frequency.

But #18 was still negative. The issue isn't just volume filtering - it's signal quality.

New hypothesis - MULTI-TIMEFRAME CONFIRMATION:
What if we're entering on noise at the 5m level? The campaign specifies "signal measured
over 5 minutes" but doesn't forbid using longer timeframes for CONFIRMATION.

Strategy:
1. Use 5m bars for the primary flow signal (campaign spec: signal over 5 minutes)
2. Add 15m confirmation: only enter if the 15m bar also shows directional agreement
3. This filters out 5m noise that contradicts the longer-term direction
4. Keep high conviction threshold: 0.57 on both timeframes
5. Keep strict volume filter: 250 on 5m (thin bars are unreliable)
6. Keep 1 entry per session (proven better than 2)
7. Keep bracket: 1.5 stop / 3.5 target

Why this might work:
- 5m signal catches the immediate flow
- 15m confirmation prevents entering against the intermediate trend
- We're still respecting the "signal over 5 minutes" requirement
- Multi-timeframe alignment is a classic way to reduce false signals
- Commission is still 0.20 per round turn on ~384 trades, but edge per trade should improve

The logic:
- Current 5m bar: >0.57 on one side
- Previous 5m bar: not strongly opposed (< 0.55)
- Current 15m bar: also >0.55 on same side (softer threshold for confirmation)
- Price action: close in upper/lower 60% of 5m bar
- This requires directional agreement across two timeframes

Risk: If 15m confirmation is too restrictive, we might filter out valid trades.
But the trial history shows our edge is fragile - better to be selective.

Expected outcome:
- Fewer total trades (maybe 250-350 instead of 384)
- Better directional alignment
- Higher edge per trade
- Score potentially in -0.12 to -0.13 range

This is a refinement of #18's logic, not a structural overhaul.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "5m",
        slow_interval: str = "15m",
        flow_threshold: float = 0.57,
        confirmation_threshold: float = 0.55,
        prev_threshold: float = 0.55,
        close_threshold: float = 0.60,
        stop_offset: float = 1.5,
        target_offset: float = 3.5,
        size: int = 1,
        min_volume: int = 250,
        max_entries_per_session: int = 1,
    ):
        """
        Args:
            fast_interval: Primary signal timeframe (5m per campaign spec)
            slow_interval: Confirmation timeframe (15m for agreement)
            flow_threshold: Primary signal requires >57% on one side
            confirmation_threshold: Confirmation requires >55% on same side (softer)
            prev_threshold: Previous bar rejection threshold (0.55)
            close_threshold: Close position in range threshold (0.60)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.5 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio on fast interval
            max_entries_per_session: Maximum entries per session
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.flow_threshold = flow_threshold
        self.confirmation_threshold = confirmation_threshold
        self.prev_threshold = prev_threshold
        self.close_threshold = close_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0

    def setup(self, ctx) -> None:
        """Subscribe to both 5m and 15m bars."""
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: multi-timeframe confirmation.
        Enter only when 5m signal is strong AND 15m bar shows directional agreement.
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count (based on fast interval)
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
        
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
        last_slow = slow_history[-1]  # Most recent 15m bar (may have just closed or earlier)
        
        # Skip first 3 bars of session (cash open microstructure is different)
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session (forced liquidation, end-of-day pressure)
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes (in nanoseconds)
            return
        
        # Skip low-volume bars on fast interval
        if last_fast.volume < self.min_volume:
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
        
        # BUY SIGNAL: Strong buy on fast interval + confirmation on slow interval
        if fast_buy_ratio > self.flow_threshold:
            # Previous fast bar check: only reject if prev was strongly sell-dominated
            if prev_sell_ratio > self.prev_threshold:
                return
            
            # Slow interval confirmation: must show buying tendency (>0.55)
            if slow_buy_ratio <= self.confirmation_threshold:
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
        
        # SELL SIGNAL: Strong sell on fast interval + confirmation on slow interval
        elif fast_sell_ratio > self.flow_threshold:
            # Previous fast bar check: only reject if prev was strongly buy-dominated
            if prev_buy_ratio > self.prev_threshold:
                return
            
            # Slow interval confirmation: must show selling tendency (>0.55)
            if slow_sell_ratio <= self.confirmation_threshold:
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
