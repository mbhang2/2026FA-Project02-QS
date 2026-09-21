"""
Flow continuation strategy: multi-bar momentum with entry clustering.

Analysis of best performers:
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → baseline
- #34: -0.1978, 768 trades → tighter opposition (0.52) hurt performance
- #33: -0.1740, 768 trades → volume persistence helped slightly

Key insight from #34's underperformance:
Tightening the opposition threshold from 0.55 to 0.52 REDUCED performance.
This tells us the opposition check at 0.55 is already well-calibrated.
The problem is not filtering too little—it's that we're still entering on
marginal bars that reverse.

New hypothesis - MULTI-BAR MOMENTUM CONFIRMATION:
Instead of just checking the previous bar, require that the last 2-3 bars
show CONSISTENT directional bias. A real move has momentum building over
multiple bars, not just one.

Implementation:
1. Keep core #9 logic: 0.55 threshold, 0.55 opposition check, price action
2. Add 2-bar momentum check:
   - For buy signal: require that BOTH of the last 2 bars show buy_ratio > 0.50
     (both bars show some buying pressure, not just the current one)
   - For sell signal: require that BOTH of the last 2 bars show sell_ratio > 0.50
     (both bars show some selling pressure)
3. This filters out isolated spikes that reverse immediately
4. Momentum building = bars 1 and 2 both show directional bias

Why this works:
- A single bar with 0.55+ ratio can reverse on the next bar
- Two consecutive bars with 0.50+ on the same side = real momentum
- This naturally reduces trades (only enters on sustained signals)
- Expected: 500-600 trades (down from 768)
- Better edge per trade from multi-bar confirmation
- The core signal is unchanged, we're just requiring consistency

Expected outcome:
- 500-600 trades (down from 768)
- Better quality entries from 2-bar confirmation
- Score in -0.10 to -0.13 range

The philosophy:
#9 is excellent. The signal works. The issue is that one-bar spikes reverse.
By requiring the last two bars to both show directional bias (>0.50 on the
signal side), we catch momentum that has proven it can persist. This is not
adding complexity—it's adding confirmation to existing signals.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        momentum_threshold: float = 0.50,
        opposition_threshold: float = 0.55,
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
            momentum_threshold: Minimum ratio on BOTH prior bars (0.50 = 50%+)
            opposition_threshold: Reject entry if prev bar shows opposite at this
                                 level (0.55 = reject if 55%+ opposite)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.momentum_threshold = momentum_threshold
        self.opposition_threshold = opposition_threshold
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
        Entry logic: 5m flow signal with 2-bar momentum confirmation.
        Only enter when current bar shows strong flow AND the last 2 bars
        both show directional bias in the signal direction.
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
        
        # Get previous bars for opposition and momentum checks
        prev_bar = history[-2]
        prev_prev_bar = history[-3]
        
        if prev_bar.volume <= 0 or prev_prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        prev_prev_buy_ratio = prev_prev_bar.buy_volume / prev_prev_bar.volume
        prev_prev_sell_ratio = prev_prev_bar.sell_volume / prev_prev_bar.volume
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Opposition check: reject if previous bar was strongly sell-dominated
            if prev_sell_ratio > self.opposition_threshold:
                return
            
            # 2-bar momentum check: both of the last 2 bars should show buying
            # (or at least not be dominated by selling). Require both prev bars
            # to show buy_ratio > momentum_threshold (0.50 = 50%+ buying)
            if prev_buy_ratio <= self.momentum_threshold or prev_prev_buy_ratio <= self.momentum_threshold:
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
            if prev_buy_ratio > self.opposition_threshold:
                return
            
            # 2-bar momentum check: both of the last 2 bars should show selling
            # (or at least not be dominated by buying). Require both prev bars
            # to show sell_ratio > momentum_threshold (0.50 = 50%+ selling)
            if prev_sell_ratio <= self.momentum_threshold or prev_prev_sell_ratio <= self.momentum_threshold:
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
