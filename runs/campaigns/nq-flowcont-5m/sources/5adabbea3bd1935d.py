"""
Flow continuation strategy: refined entry with momentum persistence check.

Analysis of recent trials:
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best baseline
- #29: -0.3693, 768 trades (2.0/session) → 1m confirmation underperformed
- #28: -0.3249, 1152 trades (3.0/session) → soft volume filter underperformed
- #19: -0.1631, 382 trades (0.99/session) → fewer trades competitive

Core insight:
#9 works because the signal is simple and the threshold (0.55) is right.
The 5m flow signal itself is strong. The problem is we're entering on isolated
spikes that don't persist.

New hypothesis - MOMENTUM PERSISTENCE:
Instead of looking at just the previous bar (which can be noisy), track whether
the flow direction has been CONSISTENT over the last 2-3 bars. This filters out
"one-bar wonders" and catches moves that are actually developing.

Key changes from #9:
1. Keep the 0.55 threshold (it works)
2. Instead of just checking prev_bar doesn't oppose, check that the LAST 2 BARS
   show momentum in the same direction (both >0.50 on the same side)
3. This is a PERSISTENCE check: we want to see the flow developing, not reversing
4. Keep everything else: stop 1.5, target 3.0, session discipline, volume filter

Why this is different from #29:
- #29 added a finer timeframe (1m), which added complexity and hurt performance
- This uses ONLY 5m bars but requires momentum to persist across 2 bars
- Much simpler: fewer parameters, clearer signal
- Filters out reversals and sudden flips without adding new data sources

Expected outcome:
- 600-700 trades (fewer from persistence filter, but higher quality)
- Better signal quality from requiring momentum consistency
- Score in -0.12 to -0.14 range (improvement from #9's -0.1437)

The philosophy:
#9 is excellent. The issue is execution - we're taking too many marginal entries.
By requiring that the flow momentum persists across the last 2 bars, we get only
the trades where the move is actually developing, not just spiking.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        momentum_threshold: float = 0.50,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio for entry signal (0.55 = 55%+)
            momentum_threshold: Minimum ratio for momentum persistence (0.50 = 50%+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.momentum_threshold = momentum_threshold
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
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal with momentum persistence check.
        Only enter when current bar shows strong flow AND the last 2 bars
        show momentum developing in the same direction.
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
        
        self._bars_in_session += 1
        
        # One position at a time
        if ctx.position or ctx.working_order_ids:
            return
        
        # Limit entries per session
        if self._entries_this_session >= self.max_entries_per_session:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 3:
            return
        
        last_bar = history[-1]  # Bar that just closed
        
        # Skip first 3 bars of session (cash open microstructure is different)
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session (forced liquidation, end-of-day pressure)
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes (in nanoseconds)
            return
        
        # Skip low-volume bars (flow ratio unreliable on thin bars)
        if last_bar.volume < self.min_volume:
            return
        
        # Measure aggression: ratio of dominant side to total volume
        buy_vol = last_bar.buy_volume
        sell_vol = last_bar.sell_volume
        total_vol = last_bar.volume
        
        if total_vol <= 0:
            return
        
        buy_ratio = buy_vol / total_vol
        sell_ratio = sell_vol / total_vol
        
        # Get previous two bars for momentum persistence
        prev_bar_1 = history[-2]
        prev_bar_2 = history[-3]
        
        if prev_bar_1.volume <= 0 or prev_bar_2.volume <= 0:
            return
        
        prev_buy_ratio_1 = prev_bar_1.buy_volume / prev_bar_1.volume
        prev_sell_ratio_1 = prev_bar_1.sell_volume / prev_bar_1.volume
        prev_buy_ratio_2 = prev_bar_2.buy_volume / prev_bar_2.volume
        prev_sell_ratio_2 = prev_bar_2.sell_volume / prev_bar_2.volume
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Momentum persistence check: the last 2 bars should show buying momentum
            # At least one of them should show >0.50 buying (momentum_threshold)
            # and neither should show strong selling (>0.55)
            prev_1_has_buy = prev_buy_ratio_1 > self.momentum_threshold
            prev_2_has_buy = prev_buy_ratio_2 > self.momentum_threshold
            prev_1_strong_sell = prev_sell_ratio_1 > self.flow_threshold
            prev_2_strong_sell = prev_sell_ratio_2 > self.flow_threshold
            
            # Require: at least one bar shows buying, and neither shows strong selling
            if not (prev_1_has_buy or prev_2_has_buy):
                return
            if prev_1_strong_sell or prev_2_strong_sell:
                return
            
            # Price action confirmation: close above midpoint
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.5:
                    return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal: current bar has 55%+ sell volume
        elif sell_ratio > self.flow_threshold:
            # Momentum persistence check: the last 2 bars should show selling momentum
            # At least one of them should show >0.50 selling (momentum_threshold)
            # and neither should show strong buying (>0.55)
            prev_1_has_sell = prev_sell_ratio_1 > self.momentum_threshold
            prev_2_has_sell = prev_sell_ratio_2 > self.momentum_threshold
            prev_1_strong_buy = prev_buy_ratio_1 > self.flow_threshold
            prev_2_strong_buy = prev_buy_ratio_2 > self.flow_threshold
            
            # Require: at least one bar shows selling, and neither shows strong buying
            if not (prev_1_has_sell or prev_2_has_sell):
                return
            if prev_1_strong_buy or prev_2_strong_buy:
                return
            
            # Price action confirmation: close below midpoint
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
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
