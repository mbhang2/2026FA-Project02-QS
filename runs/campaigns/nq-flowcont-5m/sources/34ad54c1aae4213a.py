"""
Flow continuation strategy: Asymmetric risk/reward with tighter entry discipline.

Analysis of 47 trials reveals critical pattern:
- Trial #43: -0.1370 (best so far, 768 trades)
- Trial #47: -0.1370 (momentum check, 768 trades)
- Trial #9:  -0.1437 (baseline, 768 trades)

Key insight from comparing #43 and #47: Both score -0.1370 despite different
filters. This suggests the 0.55 flow threshold + 0.67 close_position are the
core drivers, and additional filters (momentum, volume) don't improve score.

The problem: We're still negative. Commission is 0.20 per round turn, and at
768 trades we're paying ~153 points total in commission. We need to:
1. Reduce total trades (fewer commission hits)
2. Keep only the highest-conviction entries
3. Improve win rate significantly

New hypothesis - STRICTER ENTRY WITH EXTENDED TARGET:
Instead of adding filters, TIGHTEN THE CORE ENTRY CRITERIA:
1. Increase flow_threshold from 0.55 to 0.60 (only 60%+ dominant side)
2. Increase close_position_threshold from 0.67 to 0.75 (must be in outer QUARTER)
3. Keep target=4.0, stop=1.5 (proven good risk/reward)
4. Keep opposition check at 0.55 (soft floor)
5. Expected: 400-500 trades (fewer, higher conviction)
6. Score improvement to -0.08 to -0.05 range

Why this works:
- Trial #43 was best with 768 trades but still negative
- Reducing trade count by 40-50% cuts commission impact by 40-50%
- At 400 trades: ~80 points commission vs ~153 at 768 trades
- Tighter entry (0.60 flow, 0.75 close) means only STRONG bars qualify
- A bar with 60%+ flow in upper quarter is much more reliable than 55% in upper third
- Fewer trades × higher win rate = positive P&L possible
- Aligns with campaign spec: 5m signal, 5m+ hold (no time pressure)

Why we haven't tried this:
- We've been adding filters (momentum, volume) not tightening core thresholds
- Trial #45 tried 0.75 close_position but with 5.0 target (too extended)
- This combines stricter entry (0.75) with proven target (4.0)
- The 0.60 flow threshold hasn't been tested yet

Why 0.60 and 0.75 specifically:
- 0.55 → 0.60 is a 9% increase in threshold (meaningful, not arbitrary)
- 0.67 → 0.75 moves from upper THIRD to upper QUARTER (much stricter)
- These create a natural "high conviction" bar pattern
- At these levels, we're filtering for bars where BOTH flow AND price action align

Risk:
- Might reduce trades too much (< 300 could hurt due to sample size)
- But negative score at 768 trades means we NEED fewer, higher-quality trades
- The math: if we can get 50% win rate on 400 trades, we make money
- At 768 trades with ~45% win rate, we lose money despite being "right"

Implementation:
1. Keep all session tracking from trial #43
2. Increase flow_threshold to 0.60 (was 0.55)
3. Increase close_position_threshold to 0.75 (was 0.67)
4. Keep opposition_threshold at 0.55 (soft floor)
5. Keep target=4.0, stop=1.5
6. Keep min_volume=150, max_entries_per_session=2
7. Keep session/bar timing filters (first 3 bars, last 5 minutes)

Expected outcome:
- 400-500 trades (down from 768)
- Much higher conviction entries (60% flow, 75% close position)
- Better win rate from stricter criteria
- Commission impact: ~80-100 points (vs 153 at 768 trades)
- Score: -0.08 to -0.05 (improvement from -0.1370)
- Still trading enough to validate the signal, not so much that commission kills it
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.60,
        opposition_threshold: float = 0.55,
        stop_offset: float = 1.5,
        target_offset: float = 4.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
        close_position_threshold: float = 0.75,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side (0.60 = 60%+)
            opposition_threshold: Reject if prev bar opposed at this level (0.55)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (4.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
            close_position_threshold: Close must be in outer quarter (0.75)
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.opposition_threshold = opposition_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        self.close_position_threshold = close_position_threshold
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal with stricter entry criteria.
        Only highest-conviction bars: 60%+ flow, close in outer quarter.
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
        
        # Get previous bar for opposition check
        prev_bar = history[-2]
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Price action confirmation: close must be in outer quarter of range
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            return
        
        close_position = (last_bar.close - last_bar.low) / bar_range
        
        # Strong buy signal: current bar has 60%+ buy volume (stricter than 55%)
        if buy_ratio > self.flow_threshold:
            # Opposition check: previous bar shouldn't show strong selling
            if prev_sell_ratio > self.opposition_threshold:
                return
            
            # Stricter price action: close must be in OUTER QUARTER of range
            # (0.75 means in top 25% of range, not just top 33%)
            if close_position < self.close_position_threshold:
                return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal: current bar has 60%+ sell volume (stricter than 55%)
        elif sell_ratio > self.flow_threshold:
            # Opposition check: previous bar shouldn't show strong buying
            if prev_buy_ratio > self.opposition_threshold:
                return
            
            # Stricter price action: close must be in LOWER QUARTER of range
            # (close_position < 0.25 means in bottom 25% of range)
            if close_position > (1.0 - self.close_position_threshold):
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
