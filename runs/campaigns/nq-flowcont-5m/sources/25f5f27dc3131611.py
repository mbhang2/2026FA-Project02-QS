"""
Flow continuation strategy: return to #9 baseline with minimal volatility-based position sizing.

Analysis of recent trials:
- #24: -0.2466, 768 trades (2.0/session) → 1m confirmation added noise
- #23: -0.2614, 767 trades (2.0/session) → asymmetric thresholds hurt
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best so far
- #19: -0.1631, 382 trades (0.99/session) → multi-timeframe worked but fewer trades

The data is telling us something clear:
1. The best score (-0.1437) came at 0.55 threshold with 768 trades
2. Every "improvement" attempt has failed
3. Adding complexity (1m confirmation, asymmetric thresholds, etc.) makes it worse
4. The signal is working; the problem is not entry selection

New hypothesis - RETURN TO #9 WITH POSITION SIZING:
#9 was right about the core signal. What if the issue is that we're taking equal-sized
positions on all signals, but some signals are higher quality than others?

The key insight: Not all 0.55+ bars are equal.
- A bar with 65% on one side is MUCH stronger than a bar with 55% on one side
- We're treating them the same (size=1 in both cases)
- What if we size the position based on how strong the signal is?

Why this might work:
1. Stronger flow signals (0.60+) get size=1
2. Moderate signals (0.55-0.60) get size=0 (skip them)
3. This keeps the 2 trades/session target but only on HIGH CONVICTION signals
4. Expected: 700-800 trades of much better quality
5. The edge per trade improves, offsetting commission

The logic:
- Use #9's core approach: 0.55 threshold for entry detection
- BUT: only actually place the trade if buy_ratio > 0.60 or sell_ratio > 0.60
- This is stricter than #9 but only on the actual execution
- Session discipline: skip first 3 bars, last 5 minutes
- Volume filter: >150
- Previous bar: reject if strongly opposed (>0.55)
- Price action: close in upper/lower half (simple)
- Stop 1.5, Target 3.0 (same as #9)
- Max 2 entries per session (but we'll get fewer due to stricter execution)

Why this is different from just raising the threshold to 0.60:
- The detection is still at 0.55 (we see all signals)
- But execution only happens at 0.60+ (we only trade the best ones)
- This maintains awareness of the flow while being selective about action
- It's like #9 but with a hidden second gate on the actual order

Expected outcome:
- 550-700 trades (fewer than #9's 768, but much higher quality)
- Better edge per trade from signal strength
- Score in -0.10 to -0.13 range

The philosophy:
#9 worked. We're not going to beat it by adding complexity or changing the threshold.
We're going to beat it by being more selective about WHICH of those threshold-crossing
signals we actually trade on, based on signal strength.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        detection_threshold: float = 0.55,
        execution_threshold: float = 0.60,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            detection_threshold: Threshold for detecting signals (0.55 = 55%+)
            execution_threshold: Threshold for actually placing trades (0.60 = 60%+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts (for signals that pass execution threshold)
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.detection_threshold = detection_threshold
        self.execution_threshold = execution_threshold
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
        Entry logic: detect signals at 0.55 threshold, execute only at 0.60+ threshold.
        This filters for high-conviction signals while maintaining awareness of flow.
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
        
        # Get previous bar for directional agreement
        prev_bar = history[-2]
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # BUY SIGNAL DETECTION: current bar has >55% buy volume
        if buy_ratio > self.detection_threshold:
            # Previous bar check: reject if strongly sell-dominated
            if prev_sell_ratio > self.detection_threshold:
                return
            
            # Price action confirmation: close above midpoint
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.5:
                    return
            
            # EXECUTION GATE: only place trade if signal is STRONG (>60% buy)
            if buy_ratio > self.execution_threshold:
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="flow-long",
                )
                self._entries_this_session += 1
        
        # SELL SIGNAL DETECTION: current bar has >55% sell volume
        elif sell_ratio > self.detection_threshold:
            # Previous bar check: reject if strongly buy-dominated
            if prev_buy_ratio > self.detection_threshold:
                return
            
            # Price action confirmation: close below midpoint
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > 0.5:
                    return
            
            # EXECUTION GATE: only place trade if signal is STRONG (>60% sell)
            if sell_ratio > self.execution_threshold:
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
