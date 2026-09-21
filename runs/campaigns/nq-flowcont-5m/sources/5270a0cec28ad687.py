"""
Flow continuation strategy: refined multi-timeframe with 1-minute micro-confirmation.

Analysis of recent trials:
- #20: -0.1665, 361 trades (0.94/session) → volume normalization helped slightly
- #19: -0.1631, 382 trades (0.99/session) → multi-timeframe confirmation working
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best so far

Key observation:
Despite multi-timeframe confirmation and asymmetric thresholds, we're still negative.
The issue isn't just signal quality at the 5m level - it's entry timing WITHIN the bar.

New insight - INTRA-BAR TIMING WITH 1-MINUTE MICRO-CONFIRMATION:
The 5m bar closes and we enter. But what if the flow that dominated the bar is already
reversing? We're entering AFTER the move has happened, paying the spread, then the
reversal happens.

Solution: Use 1-minute bars to check that buying/selling pressure is CONTINUING
into the current 5-minute bar, not just that it dominated the previous one.

Strategy:
1. Keep 5m as primary signal (campaign spec: signal over 5 minutes)
2. Keep 15m as confirmation of intermediate direction
3. Add NEW: 1m micro-confirmation - check that the CURRENT 1m bar (not yet closed)
   shows continuation of the flow direction
4. This means: we only enter if aggression is CONTINUING, not reversing
5. Keep asymmetric thresholds: 0.56 buy, 0.58 sell
6. Keep volume normalization (session average)
7. Keep 1 entry per session
8. Tighten confirmation thresholds slightly: 0.57 on 15m, 0.54 on 1m (softer micro-check)

Why this might work:
- The 5m bar shows strong directional flow
- The 15m bar confirms we're not against the trend
- The 1m bar confirms the flow is CONTINUING (not reversing mid-bar)
- We're catching flow that's still active, not flow that's already over
- This addresses the core issue: we're entering too late in the move
- Expected: 250-350 trades with better edge per trade (higher conviction entries)

The logic:
- Current 5m bar just closed with strong flow (0.56 buy or 0.58 sell)
- Previous 5m bar wasn't strongly opposed
- Current 15m bar shows directional agreement (0.57+)
- Current 1m bar (incomplete, just started) shows micro-agreement (0.54+)
- Price action: 5m close in upper/lower 60%
- This ensures we're entering when flow is ACTIVE, not when it's over

Risk: 1m bars are noisier than 5m. But we're using a softer threshold (0.54)
and only as a continuation check, not as a primary signal. The 5m + 15m still
drive the decision; 1m is just a "is this still happening?" filter.

Expected improvement:
- Fewer false entries (flow that reversed)
- Better entry timing (entering while flow continues, not after)
- Higher win rate even if lower trade frequency
- Score potentially in -0.12 to -0.14 range
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "5m",
        mid_interval: str = "15m",
        micro_interval: str = "1m",
        buy_threshold: float = 0.56,
        sell_threshold: float = 0.58,
        mid_confirmation_threshold: float = 0.57,
        micro_confirmation_threshold: float = 0.54,
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
            mid_interval: Intermediate confirmation (15m for trend)
            micro_interval: Micro confirmation (1m for continuation check)
            buy_threshold: Buy signal requires >56% buy volume
            sell_threshold: Sell signal requires >58% sell volume
            mid_confirmation_threshold: Mid-term requires >57% on same side
            micro_confirmation_threshold: Micro requires >54% on same side (softer)
            prev_threshold: Previous bar rejection threshold (0.55)
            close_threshold: Close position in range threshold (0.60)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.5 points)
            size: Position size in contracts
            max_entries_per_session: Maximum entries per session
        """
        self.fast_interval = fast_interval
        self.mid_interval = mid_interval
        self.micro_interval = micro_interval
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.mid_confirmation_threshold = mid_confirmation_threshold
        self.micro_confirmation_threshold = micro_confirmation_threshold
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
        """Subscribe to 5m, 15m, and 1m bars."""
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.mid_interval)
        ctx.subscribe(self.micro_interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: multi-timeframe confirmation with micro-level continuation check.
        Enter when 5m signal is strong, 15m confirms direction, and 1m shows continuation.
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
        
        mid_history = ctx.history(self.mid_interval)
        if len(mid_history) < 1:
            return
        
        micro_history = ctx.history(self.micro_interval)
        if len(micro_history) < 1:
            return
        
        last_fast = fast_history[-1]  # 5m bar that just closed
        last_mid = mid_history[-1]    # Most recent 15m bar
        last_micro = micro_history[-1] # Most recent 1m bar (may be current/incomplete)
        
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
        
        # Measure aggression on fast interval (5m - the closed bar)
        fast_buy_vol = last_fast.buy_volume
        fast_sell_vol = last_fast.sell_volume
        fast_total_vol = last_fast.volume
        
        if fast_total_vol <= 0:
            return
        
        fast_buy_ratio = fast_buy_vol / fast_total_vol
        fast_sell_ratio = fast_sell_vol / fast_total_vol
        
        # Measure aggression on mid interval (15m - confirmation)
        mid_buy_vol = last_mid.buy_volume
        mid_sell_vol = last_mid.sell_volume
        mid_total_vol = last_mid.volume
        
        if mid_total_vol <= 0:
            return
        
        mid_buy_ratio = mid_buy_vol / mid_total_vol
        mid_sell_ratio = mid_sell_vol / mid_total_vol
        
        # Measure aggression on micro interval (1m - continuation check)
        micro_buy_vol = last_micro.buy_volume
        micro_sell_vol = last_micro.sell_volume
        micro_total_vol = last_micro.volume
        
        if micro_total_vol <= 0:
            return
        
        micro_buy_ratio = micro_buy_vol / micro_total_vol
        micro_sell_ratio = micro_sell_vol / micro_total_vol
        
        # Get previous fast bar for directional agreement
        prev_fast = fast_history[-2]
        if prev_fast.volume <= 0:
            return
        
        prev_buy_ratio = prev_fast.buy_volume / prev_fast.volume
        prev_sell_ratio = prev_fast.sell_volume / prev_fast.volume
        
        # BUY SIGNAL: 5m strong buy + 15m confirmation + 1m continuation
        if fast_buy_ratio > self.buy_threshold:
            # Previous fast bar check: only reject if prev was strongly sell-dominated
            if prev_sell_ratio > self.prev_threshold:
                return
            
            # Mid-term confirmation: must show buying tendency
            if mid_buy_ratio <= self.mid_confirmation_threshold:
                return
            
            # Micro continuation: must show buying pressure continuing
            if micro_buy_ratio <= self.micro_confirmation_threshold:
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
        
        # SELL SIGNAL: 5m strong sell + 15m confirmation + 1m continuation
        elif fast_sell_ratio > self.sell_threshold:
            # Previous fast bar check: only reject if prev was strongly buy-dominated
            if prev_buy_ratio > self.prev_threshold:
                return
            
            # Mid-term confirmation: must show selling tendency
            if mid_sell_ratio <= self.mid_confirmation_threshold:
                return
            
            # Micro continuation: must show selling pressure continuing
            if micro_sell_ratio <= self.micro_confirmation_threshold:
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
