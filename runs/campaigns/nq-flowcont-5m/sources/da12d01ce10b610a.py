"""
Flow continuation strategy with refined entry timing and realistic expectation setting.

Analysis of prior trials:
- #4: -0.2765, 768 trades (2.0/session) → best so far
- #5: -0.2961, 384 trades (1.0/session) → worse, too restrictive

Key insight: #4 outperforms #5 despite having 2x the trades. This suggests that
being TOO restrictive (1 entry/session) loses more edge than it gains in reduced
whipsaws. The sweet spot appears to be around 2 entries per session with high
conviction, not 1.

Problem analysis:
1. #5's single entry per session is leaving money on the table. The session has
   multiple valid flow continuation opportunities.

2. The 3-bar flow agreement requirement in #5 may be too strict. It requires
   consensus when the signal might be strongest on the current bar alone with
   just directional non-contradiction from the previous bar.

3. Price action confirmation (close position, new high/low) adds complexity.
   The core signal is flow - we should trust it when it's strong enough.

4. The 0.60 flow threshold in #5 may be filtering out tradeable signals. #4's
   0.55 threshold with 2 entries per session was better.

5. Risk/reward of 2.0 stop / 4.0 target is conservative but may be TOO wide.
   At 0.25 tick size, a 4.0 point target requires a 16-tick move. A 2.0 stop
   is 8 ticks of buffer. Maybe 1.5/3.0 was closer to optimal.

This iteration returns to #4's core parameters but with refinements:

1. **Flow threshold back to 0.55**: Trust stronger flow signals without requiring
   absolute dominance. 55%+ of volume on one side is statistically significant.

2. **Simpler confirmation logic**: Previous bar should not contradict (not strongly
   opposite), not require full agreement. This is less restrictive than #5 but
   more selective than #3.

3. **2 entries per session**: #4's sweet spot. Allows multiple setups while
   maintaining discipline.

4. **Return to 1.5/3.0 bracket**: Better risk/reward ratio than 2.0/4.0. At
   commission of 0.20 per round turn, we need tighter stops to be profitable.

5. **Moderate volume threshold (100)**: Avoid obvious thin bars but don't over-filter.

6. **Skip first 2 bars, last 5 minutes**: Balance between avoiding overnight gap
   noise and end-of-session pressure without being overly conservative.

7. **Close position confirmation**: Simple check that close is on the right side
   of the bar (60% for longs, 40% for shorts). This is price action validation
   without being too strict.

The hypothesis: Flow continuation works best when we're selective but not obsessive.
#4's parameters were close to optimal; we refine the logic slightly without
chasing ever-lower trade counts.
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
                           0.55 = 55%+ of volume on one side
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume to ensure reliable flow ratio
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
        self._current_session = None
        self._entries_this_session = 0
        self._bars_in_session = 0

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: flow continuation signals with simple directional confirmation
        and price action validation.
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
        
        # Skip first 2 bars of session (overnight gap, different microstructure)
        if self._bars_in_session <= 2:
            return
        
        # Skip last 5 minutes of session (forced liquidation, end-of-day pressure)
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
        
        # Get previous bar for confirmation
        prev_bar = history[-2]
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume if prev_bar.volume > 0 else 0.5
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume if prev_bar.volume > 0 else 0.5
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Directional confirmation: previous bar should not be strongly sell-biased
            # (allow neutral or buy-biased, reject if previous was sell-dominated)
            if prev_sell_ratio > 0.55:
                return
            
            # Price action confirmation: close should be in upper 60% of bar range
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.60:
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
            # Directional confirmation: previous bar should not be strongly buy-biased
            if prev_buy_ratio > 0.55:
                return
            
            # Price action confirmation: close should be in lower 40% of bar range
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > 0.40:
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
