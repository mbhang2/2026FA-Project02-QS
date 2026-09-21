"""
Flow continuation strategy: Adaptive flow intensity with dynamic hold period.

Analysis of 44 trials reveals that the best score is -0.1370 (trial #43).
Key observations from the trial history:

1. Trial #9: -0.1437 (baseline: 0.55 threshold, 3.0 target, 1.5 stop, 768 trades)
2. Trial #43: -0.1370 (extended target 4.0, stricter close_position 0.67, 768 trades)
3. Trial #44: -0.2384 (2-bar confirmation, 767 trades) - WORSE than #43

Critical insight: Trial #43 IMPROVED over #9 despite having MORE negative score.
Wait - that's backwards. -0.1370 is BETTER (higher) than -0.1437.
So extending the target to 4.0 and adding stricter price action HELPED.

But trial #44 with 2-bar confirmation got -0.2384, which is much WORSE.
This suggests:
- 2-bar confirmation filters out too many good trades
- Extended holds (4.0 target) are better than shorter ones
- The issue is NOT reducing trade count but improving QUALITY of remaining trades

New hypothesis - VARIABLE INTENSITY ENTRY FILTER:
The 0.55 threshold is a binary gate. But flow intensity varies:
- A bar with 65% buy volume is a much stronger signal than 55%
- Maybe we should WEIGHT entries by flow intensity
- Or use a TIERED approach: high intensity (70%+) enters with extended target,
  medium intensity (55-70%) enters with standard target

But actually, looking at trial #43 more carefully:
- It added close_position_threshold = 0.67 (stricter price action)
- It increased target to 4.0
- It scored -0.1370 (BETTER than -0.1437)

So the improvements were:
1. Stricter entry (close in outer third, not just any side)
2. Extended target (capture more of the move)

What if we go FURTHER in this direction?

New approach - EVEN STRICTER ENTRY WITH EVEN LONGER TARGET:
1. Keep 0.55 base flow threshold
2. Increase close_position_threshold to 0.75 (close in OUTER QUARTER, very tight)
3. Increase target_offset to 5.0 (even longer hold)
4. Keep opposition check
5. Keep volume filter
6. Expected: 500-600 trades with much higher conviction
7. Score improvement to -0.10 to -0.08 range

Why this might work:
- Trial #43 showed that STRICTER entry + LONGER target = BETTER score
- Extrapolating: even stricter entry + even longer target = even better
- Very tight close position (0.75) means extreme conviction
- 5.0 target lets the flow move fully develop
- Fewer trades but much higher quality
- Better commission economics (fewer total round trips)

Why we haven't tried this:
- Natural progression: #9 -> #43 (slightly stricter) -> now (much stricter)
- Risk/reward becomes 1:3.33, very asymmetric in our favor
- Commission impact is real, but fewer high-conviction trades beats many mediocre ones

Implementation:
1. Keep flow_threshold at 0.55 (proven)
2. Increase close_position_threshold to 0.75 (very strict)
3. Increase target_offset to 5.0 (let winners run)
4. Keep stop_offset at 1.5 (unchanged)
5. Keep opposition check and volume filter
6. Keep max_entries_per_session at 2

Expected outcome:
- 400-500 trades (much more selective than 768)
- Much higher conviction entries (close in outer quarter)
- Better risk/reward (1:3.33)
- Score: -0.10 to -0.08 (improvement from -0.1370)
- Fewer but better quality trades
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        opposition_threshold: float = 0.55,
        stop_offset: float = 1.5,
        target_offset: float = 5.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
        close_position_threshold: float = 0.75,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side (0.55 = 55%+)
            opposition_threshold: Reject if prev bar opposed at this level (0.55)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (5.0 points, extended further)
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
        Entry logic: 5m flow signal with very strict price action confirmation.
        Extended target to capture the full flow-driven move.
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
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Opposition check: previous bar shouldn't show strong selling
            if prev_sell_ratio > self.opposition_threshold:
                return
            
            # Very strict price action: close must be in OUTER QUARTER of range
            # (close_position >= 0.75). Requires extreme buying pressure.
            if close_position < self.close_position_threshold:
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
            # Opposition check: previous bar shouldn't show strong buying
            if prev_buy_ratio > self.opposition_threshold:
                return
            
            # Very strict price action: close must be in OUTER QUARTER of range
            # (close_position <= 0.25). Requires extreme selling pressure.
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
