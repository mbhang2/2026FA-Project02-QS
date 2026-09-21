"""
Flow continuation strategy: multi-timeframe confirmation with volume persistence.

Analysis of best performers:
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → baseline
- #32: -0.2350, 768 trades (2.0/session) → momentum filter underperformed
- #31: -0.2035, 768 trades (2.0/session) → adaptive threshold underperformed

Core insight from #9:
The signal works. The 0.55 threshold with previous bar opposition check and
simple price action is solid. The problem is NOT the signal—it's that we're
entering on marginal bars and getting whipsawed.

New hypothesis - VOLUME PERSISTENCE FILTER:
Instead of just checking the previous bar's flow ratio, check if volume is
BUILDING in the signal direction. A real move has conviction reflected in volume.

Implementation:
1. Keep core #9 logic: 0.55 threshold, opposition check, price action
2. Add volume persistence check:
   - For buy signal: require current bar volume > previous bar volume
     (conviction = more aggressive buying is happening)
   - For sell signal: require current bar volume > previous bar volume
     (conviction = more aggressive selling is happening)
3. This filters out "strong ratio on low volume" which is noise
4. Volume building is a real signal of momentum, not curve-fitting

Why this works:
- A bar with 0.55+ buy ratio on 100 contracts is different from 500 contracts
- Volume building in the signal direction = real conviction
- This naturally reduces trades (only enters when volume accelerates)
- Expected: 600-700 trades, better quality
- The signal still works, we're just filtering noise

Expected outcome:
- 600-700 trades (down from 768)
- Better edge per trade from volume-confirmed entries
- Score in -0.10 to -0.13 range

The philosophy:
#9 is the baseline. The core signal is sound. We reduce marginal entries by
requiring volume to build in the signal direction. This is not adding complexity—
it's adding conviction to existing signals.
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
        min_volume: int = 150,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio for entry signal (0.55 = 55%+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
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
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal with volume persistence confirmation.
        Only enter when current bar shows strong flow AND volume is building
        in the signal direction (conviction filter).
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
        
        # Get previous bar for opposition and volume checks
        prev_bar = history[-2]
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Opposition check: reject if previous bar was strongly sell-dominated
            if prev_sell_ratio > self.flow_threshold:
                return
            
            # Volume persistence check: current bar volume must exceed previous bar
            # This ensures conviction is building, not just a ratio on thin volume
            if last_bar.volume <= prev_bar.volume:
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
            # Opposition check: reject if previous bar was strongly buy-dominated
            if prev_buy_ratio > self.flow_threshold:
                return
            
            # Volume persistence check: current bar volume must exceed previous bar
            # This ensures conviction is building in the sell direction
            if last_bar.volume <= prev_bar.volume:
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
