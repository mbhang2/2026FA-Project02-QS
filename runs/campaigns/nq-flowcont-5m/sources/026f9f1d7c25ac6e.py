"""
Flow continuation strategy: asymmetric thresholds with directional bias and tighter confirmation.

Analysis of recent trials:
- #22: -0.2000, 310 trades (0.81/session) → strict 0.57/0.58 thresholds too tight
- #21: -0.2980, 316 trades (0.82/session) → 1m micro-confirmation added noise
- #19: -0.1631, 382 trades (0.99/session) → multi-timeframe confirmation working
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best so far

Critical insight from the data:
The best score (-0.1437) came at 0.55 threshold with 768 trades (2.0/session).
Recent attempts to improve by reducing trade count or raising thresholds have all failed.
This suggests:
1. The signal quality at 0.55 is actually quite good
2. More trades are better than fewer trades (higher conviction not the answer)
3. The problem is not entry selection but something else

New hypothesis - RETURN TO SIMPLICITY WITH ASYMMETRIC DIRECTIONAL BIAS:
The market is not symmetric. NQ has structural bid pressure from passive flows.
- Buy signals should be easier to trigger (0.54) since they align with structural bias
- Sell signals should be harder (0.56) since they fight structural bias
- This matches real market microstructure: it's easier to go with the flow

Why this might work:
- #9 at -0.1437 was close to this (0.55 symmetric)
- Going asymmetric could capture more of the easier direction
- Keep the 15m confirmation for robustness
- Keep volume filter and session discipline
- Expected: 800-1000 trades with slightly better edge = better score

The logic:
- BUY: 5m bar must show >54% buy (easier threshold, structural bias)
- SELL: 5m bar must show >56% sell (harder threshold, fighting bias)
- 15m confirmation: >55% on same side (moderate confirmation)
- Previous bar: don't reject if <0.55 (softer previous bar check)
- Price action: close in upper/lower 55% (slightly looser than before)
- Volume: minimum 120 absolute (not session-relative, simpler)
- 2 entries per session (proven better than 1)
- Skip first 3 bars, last 5 minutes (session discipline)

Expected outcome:
- 800-900 trades (more than #22's 310, closer to #9's 768)
- Better edge from asymmetric directional bias
- Score in -0.13 to -0.15 range

The core insight:
We've been trying to make the signal HARDER (stricter thresholds, more confirmation).
But the data says the opposite: the best result came with EASIER signals and MORE trades.
The answer is not to be more selective; it's to be more active on the right side
and slightly less active on the wrong side.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "5m",
        slow_interval: str = "15m",
        buy_threshold: float = 0.54,
        sell_threshold: float = 0.56,
        slow_confirmation_threshold: float = 0.55,
        prev_threshold: float = 0.55,
        close_threshold: float = 0.55,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 120,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            fast_interval: Primary signal timeframe (5m per campaign spec)
            slow_interval: Confirmation timeframe (15m for trend agreement)
            buy_threshold: Buy signal requires >54% buy volume (easier, structural bias)
            sell_threshold: Sell signal requires >56% sell volume (harder, against bias)
            slow_confirmation_threshold: Slow bar requires >55% on same side
            prev_threshold: Previous bar rejection threshold (0.55)
            close_threshold: Close position in range threshold (0.55 = above/below midpoint)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume (absolute, not relative)
            max_entries_per_session: Maximum entries per session
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.slow_confirmation_threshold = slow_confirmation_threshold
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
        """Subscribe to 5m and 15m bars."""
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: asymmetric thresholds with directional bias.
        Easier to go long (structural bid pressure), harder to go short (against flow).
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
        last_slow = slow_history[-1]  # Most recent 15m bar
        
        # Skip first 3 bars of session (cash open microstructure is different)
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session (forced liquidation, end-of-day pressure)
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes (in nanoseconds)
            return
        
        # Volume filter: absolute minimum volume
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
        
        # BUY SIGNAL: Asymmetric - easier threshold (0.54), structural bias
        if fast_buy_ratio > self.buy_threshold:
            # Previous fast bar check: only reject if prev was strongly sell-dominated
            if prev_sell_ratio > self.prev_threshold:
                return
            
            # Slow interval confirmation: must show buying tendency
            if slow_buy_ratio <= self.slow_confirmation_threshold:
                return
            
            # Price action confirmation on fast bar: close above midpoint
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
        
        # SELL SIGNAL: Asymmetric - harder threshold (0.56), against structural bias
        elif fast_sell_ratio > self.sell_threshold:
            # Previous fast bar check: only reject if prev was strongly buy-dominated
            if prev_buy_ratio > self.prev_threshold:
                return
            
            # Slow interval confirmation: must show selling tendency
            if slow_sell_ratio <= self.slow_confirmation_threshold:
                return
            
            # Price action confirmation on fast bar: close below midpoint
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
