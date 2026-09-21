"""
Flow continuation strategy: asymmetric thresholds with dynamic volume normalization.

Analysis of recent trials:
- #19: -0.1631, 382 trades (0.99/session) → multi-timeframe helped slightly
- #18: -0.1695, 384 trades (1.0/session) → reduced frequency improved score
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best so far

Key observation:
The multi-timeframe approach in #19 reduced trades from 384 to 382 with a small score
improvement (-0.1631 vs -0.1695). This suggests the 15m confirmation is working but
perhaps too loose. We're still negative because our edge per trade is marginal.

Critical insight from commission math:
- At 382 trades with 0.20 commission per round turn: ~38.2 index points in commission
- Our score of -0.1631 means gross P&L is roughly -0.0631 per trade
- We need to get to +0.10 to +0.15 per trade to be profitable after commission

New hypothesis - ASYMMETRIC THRESHOLDS WITH VOLUME NORMALIZATION:
The flow signal isn't uniformly reliable. Buy-side aggression might persist differently
than sell-side aggression. Also, flow ratios are noisy on bars with different volumes.

What if we:
1. Use asymmetric thresholds: 0.56 for buy (easier to trigger) vs 0.58 for sell (harder)
   - This reflects that different sides may have different predictive power
   - Buy aggression seems to lead price more reliably
2. Normalize volume filter by session context: instead of fixed 250, use session's
   average bar volume as baseline
3. Keep multi-timeframe confirmation (it helped)
4. Tighten 15m confirmation: require 0.56+ instead of 0.55+
5. Add a new check: only enter if this bar's volume is above session average
   (thick bars have more reliable flow)
6. Keep 1 entry per session (proven better than 2)

Why this might work:
- Asymmetric thresholds allow the strategy to exploit directional differences in flow
- Volume normalization adapts to session conditions (busy vs quiet sessions)
- We're trading thicker bars with more reliable flow signals
- Multi-timeframe confirmation still filters noise
- Expected: 300-350 trades with better edge per trade

The logic:
- Build session average volume in on_bars as we see bars
- Buy: fast >0.56 AND fast_volume > session_avg AND slow >0.56
- Sell: fast >0.58 AND fast_volume > session_avg AND slow >0.56
- Keep everything else from #19 (timing, price action, bracket)

Risk: If asymmetry is wrong-way (sell is actually more predictive), we'll miss
those trades. But the trial history suggests we're over-trading - being selective
is better than being frequent.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        fast_interval: str = "5m",
        slow_interval: str = "15m",
        buy_threshold: float = 0.56,
        sell_threshold: float = 0.58,
        confirmation_threshold: float = 0.56,
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
            slow_interval: Confirmation timeframe (15m for agreement)
            buy_threshold: Buy signal requires >56% buy volume (lower threshold)
            sell_threshold: Sell signal requires >58% sell volume (higher threshold)
            confirmation_threshold: Confirmation requires >56% on same side
            prev_threshold: Previous bar rejection threshold (0.55)
            close_threshold: Close position in range threshold (0.60)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.5 points)
            size: Position size in contracts
            max_entries_per_session: Maximum entries per session
        """
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.confirmation_threshold = confirmation_threshold
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
        self._session_avg_volume: float = 200.0  # Default fallback

    def setup(self, ctx) -> None:
        """Subscribe to both 5m and 15m bars."""
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: asymmetric thresholds with volume-normalized filtering.
        Enter when fast signal is strong, volume is above session average,
        and slow interval confirms direction.
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
        # This ensures we're trading on solid participation
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
        
        # BUY SIGNAL: Asymmetric lower threshold (0.56) for buy side
        # Buy aggression seems more predictive, so we're more aggressive entering longs
        if fast_buy_ratio > self.buy_threshold:
            # Previous fast bar check: only reject if prev was strongly sell-dominated
            if prev_sell_ratio > self.prev_threshold:
                return
            
            # Slow interval confirmation: must show buying tendency
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
        
        # SELL SIGNAL: Asymmetric higher threshold (0.58) for sell side
        # Sell aggression is less predictive, so we require stronger confirmation
        elif fast_sell_ratio > self.sell_threshold:
            # Previous fast bar check: only reject if prev was strongly buy-dominated
            if prev_buy_ratio > self.prev_threshold:
                return
            
            # Slow interval confirmation: must show selling tendency
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
