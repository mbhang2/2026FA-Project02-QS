"""
Flow continuation strategy with aggressive filtering and minimal position holding.

Analysis of prior trials:
- #0: -0.6012, 3224 trades (8.4/session) → too frequent
- #1: -0.7100, 5061 trades (13.2/session) → worse, more frequent
- #2: -0.7985, 65608 trades (171/session) → catastrophic over-trading
- #3: -0.3902, 1152 trades (3.0/session) → best so far

Key insight from #3 being best: **fewer, higher-conviction trades are better**.
At 0.20 points commission per round turn and 0.25 tick size, we need to be 
extremely selective. #3 achieved 3.0 trades per session and -0.3902 score.

This iteration pushes selectivity even further:

1. **Ultra-high flow threshold (0.55)**: Only the strongest flow signals qualify.
   This reduces noise and whipsaws, cutting entry frequency dramatically.

2. **Stricter directional confirmation**: 
   - For long signals: require price to close above 60% of bar range
   - For short signals: require price to close below 40% of bar range
   - This filters out fake breakouts and momentum reversals

3. **Multi-bar flow confirmation**: Don't enter on a single strong bar.
   Require the previous bar to also show directional agreement (not necessarily
   strong, just not contradicting). This avoids one-bar anomalies.

4. **Wider, asymmetric brackets**:
   - Stop: 1.5 points (absorb more noise, reduce false stops)
   - Target: 3.0 points (capture larger moves, better risk/reward)
   - This 1:2 risk/reward ratio is more favorable than prior attempts

5. **Maximum 1-2 entries per session**: Even more restrictive than #3's 3.
   This ensures we're only taking the absolute best setups.

6. **Skip first and last bars of session**: First bar has different microstructure
   (overnight gap absorption), last bar has forced liquidation pressure.

7. **Minimum volume threshold (100 contracts)**: Avoid thin bars where flow
   ratios are unreliable due to small sample sizes.

The hypothesis: flow continuation is real, but only on very strong signals
that are confirmed by price action and the preceding bar's direction.
Over-trading destroys the edge; under-trading with high conviction preserves it.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 100,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side to total volume (0-1)
                           0.55 = only 55%+ of volume on one side
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume to ensure reliable flow ratio
            max_entries_per_session: Maximum entries per session (strict limit)
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        
        # Track entries per session
        self._current_session = None
        self._entries_this_session = 0
        self._bars_in_session = 0

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: ultra-high conviction flow signals with multi-bar confirmation.
        Exit via asymmetric bracket: 1.5 stop, 3.0 target (2:1 risk/reward).
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
        if len(history) < 2:
            return
        
        last_bar = history[-1]  # Bar that just closed
        
        # Skip first bar of session (overnight gap, different microstructure)
        if self._bars_in_session <= 1:
            return
        
        # Skip last bar of session (forced liquidation, end-of-day pressure)
        # Estimate: if less than 5 minutes left in session, likely near close
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes (in nanoseconds)
            return
        
        # Skip low-volume bars
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
        
        prev_bar = history[-2]
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume if prev_bar.volume > 0 else 0.5
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume if prev_bar.volume > 0 else 0.5
        
        # Ultra-high threshold: only strongest signals
        if buy_ratio > self.flow_threshold:
            # Strong buy-side aggression
            
            # Directional confirmation: close must be in upper 60% of bar range
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.60:
                    # Close not high enough for a buy signal
                    return
            
            # Multi-bar confirmation: previous bar should not be dominated by sells
            # (it's OK if it's neutral or also buy-heavy, but not sell-heavy)
            if prev_sell_ratio > 0.55:
                # Previous bar was sell-heavy; contradicts the buy signal
                return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        elif sell_ratio > self.flow_threshold:
            # Strong sell-side aggression
            
            # Directional confirmation: close must be in lower 40% of bar range
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > 0.40:
                    # Close not low enough for a sell signal
                    return
            
            # Multi-bar confirmation: previous bar should not be dominated by buys
            if prev_buy_ratio > 0.55:
                # Previous bar was buy-heavy; contradicts the sell signal
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
