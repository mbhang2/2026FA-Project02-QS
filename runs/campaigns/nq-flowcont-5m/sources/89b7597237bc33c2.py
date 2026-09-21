"""
Flow continuation strategy: return to simplicity with refined micro-confirmation.

Analysis of recent trials:
- #23: -0.2614, 767 trades (2.0/session) → asymmetric thresholds hurt performance
- #22: -0.2000, 310 trades (0.81/session) → strict thresholds too tight
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best so far
- #19: -0.1631, 382 trades (0.99/session) → multi-timeframe working

Critical insight:
The best score (-0.1437) came with SIMPLE symmetric thresholds at 0.55 with 768 trades.
Every attempt to "improve" by adding complexity or changing thresholds has failed.
The asymmetric approach (#23) was a regression from -0.1631 to -0.2614.

Going back to the #9 approach but with ONE key refinement:
Instead of 5m + 15m confirmation, use 5m + 1m micro-confirmation.

Why this might work:
1. #9's core logic was sound (0.55 threshold, 2 trades/session, simple rules)
2. The 1m bars are NOT too noisy if used correctly - they should CONFIRM, not filter
3. A 1m bar showing the same direction IMMEDIATELY AFTER entry signal is strong confirmation
4. This is faster feedback than 15m (which can be stale by 10 minutes)
5. We're not filtering on 1m; we're confirming the 5m signal with 1m follow-through

The logic:
- 5m bar shows >55% on one side (the main signal)
- NEXT 1m bar (or current 1m if we're at the boundary) shows >55% on same side
  (micro-confirmation that the move is continuing)
- Previous 5m bar check: reject if strongly opposed (>0.55)
- Price action: close in upper/lower half (not too strict)
- Volume: >150 (simple absolute threshold)
- Session discipline: skip first 3 bars, last 5 minutes
- 2 entries per session (proven best)
- Stop 1.5, Target 3.0 (same as #9)

Why 1m confirmation instead of 15m:
- 15m is stale: by the time it confirms, 10 minutes have passed
- 1m is immediate: if the signal is real, the next minute should show continuation
- If the 5m bar is real aggression, the 1m bars within/after it should show it
- This filters whipsaws: a false 5m bar won't have 1m follow-through
- But it doesn't over-filter: real signals will have 1m confirmation

Expected outcome:
- 700-800 trades (similar to #9's 768)
- Better edge from immediate micro-confirmation filtering
- Score in -0.13 to -0.14 range

The core philosophy:
Return to what worked (#9), but add ONE layer of confirmation that doesn't
add noise - immediate 1m follow-through of the 5m signal. This is confirmation,
not over-filtering.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval_5m: str = "5m",
        interval_1m: str = "1m",
        flow_threshold: float = 0.55,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            interval_5m: Primary signal timeframe (5m per campaign spec)
            interval_1m: Micro-confirmation timeframe (1m for immediate follow-through)
            flow_threshold: Minimum ratio of dominant side (0.55 = 55%+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval_5m = interval_5m
        self.interval_1m = interval_1m
        self.flow_threshold = flow_threshold
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
        """Subscribe to 5m and 1m bars."""
        ctx.subscribe(self.interval_5m)
        ctx.subscribe(self.interval_1m)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal with 1m micro-confirmation.
        Only enter when 5m bar shows strong flow AND 1m bar confirms continuation.
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count (based on 5m interval)
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
        
        # Increment bar count only on 5m interval
        if self.interval_5m in ctx.bars:
            self._bars_in_session += 1
        
        # One position at a time
        if ctx.position or ctx.working_order_ids:
            return
        
        # Limit entries per session
        if self._entries_this_session >= self.max_entries_per_session:
            return
        
        # Only process on 5m interval closes
        if self.interval_5m not in ctx.bars:
            return
        
        history_5m = ctx.history(self.interval_5m)
        if len(history_5m) < 2:
            return
        
        history_1m = ctx.history(self.interval_1m)
        if len(history_1m) < 1:
            return
        
        last_5m = history_5m[-1]  # 5m bar that just closed
        last_1m = history_1m[-1]  # Most recent 1m bar
        
        # Skip first 3 bars of session (cash open microstructure is different)
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session (forced liquidation, end-of-day pressure)
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes (in nanoseconds)
            return
        
        # Skip low-volume bars (flow ratio unreliable on thin bars)
        if last_5m.volume < self.min_volume:
            return
        
        # Measure aggression on 5m bar
        buy_vol_5m = last_5m.buy_volume
        sell_vol_5m = last_5m.sell_volume
        total_vol_5m = last_5m.volume
        
        if total_vol_5m <= 0:
            return
        
        buy_ratio_5m = buy_vol_5m / total_vol_5m
        sell_ratio_5m = sell_vol_5m / total_vol_5m
        
        # Measure aggression on 1m bar (for confirmation)
        buy_vol_1m = last_1m.buy_volume
        sell_vol_1m = last_1m.sell_volume
        total_vol_1m = last_1m.volume
        
        if total_vol_1m <= 0:
            return
        
        buy_ratio_1m = buy_vol_1m / total_vol_1m
        sell_ratio_1m = sell_vol_1m / total_vol_1m
        
        # Get previous 5m bar for directional agreement check
        prev_5m = history_5m[-2]
        if prev_5m.volume <= 0:
            return
        
        prev_buy_ratio = prev_5m.buy_volume / prev_5m.volume
        prev_sell_ratio = prev_5m.sell_volume / prev_5m.volume
        
        # BUY SIGNAL: 5m bar shows >55% buy volume
        if buy_ratio_5m > self.flow_threshold:
            # Previous 5m bar check: reject if strongly sell-dominated
            if prev_sell_ratio > self.flow_threshold:
                return
            
            # 1m micro-confirmation: must show buying pressure (>55%)
            if buy_ratio_1m <= self.flow_threshold:
                return
            
            # Price action confirmation on 5m bar: close above midpoint
            bar_range = last_5m.high - last_5m.low
            if bar_range > 0:
                close_position = (last_5m.close - last_5m.low) / bar_range
                if close_position < 0.5:
                    return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # SELL SIGNAL: 5m bar shows >55% sell volume
        elif sell_ratio_5m > self.flow_threshold:
            # Previous 5m bar check: reject if strongly buy-dominated
            if prev_buy_ratio > self.flow_threshold:
                return
            
            # 1m micro-confirmation: must show selling pressure (>55%)
            if sell_ratio_1m <= self.flow_threshold:
                return
            
            # Price action confirmation on 5m bar: close below midpoint
            bar_range = last_5m.high - last_5m.low
            if bar_range > 0:
                close_position = (last_5m.close - last_5m.low) / bar_range
                if close_position > 0.5:
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
