"""
Flow continuation strategy: testing three-bar persistence and tighter filtering.

Analysis of prior trials:
- #10: -0.1437, 768 trades (2.0/session) → best so far
- #9: -0.1437, 768 trades (2.0/session) → tied with #10
- #8: -0.1829, 768 trades (2.0/session) → previous best

Key insight from #10's performance:
The bar persistence requirement (both current and previous bar showing >0.52
on same side) helped slightly, but we're still negative. The issue is clear:
commission is 0.20 per round turn, and at 2 trades/session we're paying ~0.10
in commission per session. We need an edge of at least 0.10-0.15 per trade
to break even, and we're not there yet.

The remaining problem: we're still entering on too many marginal signals.
#10 requires prev bar >0.52, but what if BOTH bars are just barely above
the threshold? That's not really "persistence" - that's two marginal signals.

New hypothesis for this iteration:
**Require three-bar agreement, not two.** If we see a buy bar at 0.56+, check
the previous bar at 0.52+, AND check the bar before that at 0.52+. This means
we're only entering when we see a genuine trend in the flow signal, not just
two consecutive bars that happen to both be slightly above threshold.

Additional refinements:
1. **Three-bar persistence**: Current bar >0.56, previous bar >0.52, bar-before-that >0.52
2. **Stricter price action**: Close must be in upper/lower QUARTER (0.75 threshold)
   not THIRD. This is more selective but the persistence filter lets us be strict.
3. **Reduce target slightly**: 3.0 instead of 3.5. With three-bar confirmation,
   we're entering later in the move, so we don't need as much room to run.
4. **Keep stop at 1.5**: Risk/reward becomes 1:2.0, which is reasonable.
5. **Reduce max entries to 1 per session**: With higher conviction, we should trade
   less frequently but with better quality.

The logic: By requiring three bars of agreement, we're only entering when the
flow signal is genuinely persistent and building, not when it's just two bars
above a threshold. This should significantly reduce whipsaws and false entries.

This is a more aggressive filter, but the math requires it. We need fewer,
higher-conviction trades to overcome commission. Three-bar persistence is a
real signal of continuation, not just statistical noise.

Risk: We'll trade much less (maybe 0.5-1.0 trades per session instead of 2).
But those trades should have much higher edge. If the hypothesis is right,
we should see better score with fewer total trades.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.56,
        persistence_threshold: float = 0.52,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 1,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side for current bar (0.56 = 56%+)
            persistence_threshold: Previous bars should show agreement (0.52+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.persistence_threshold = persistence_threshold
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
        Entry logic: flow continuation with three-bar persistence requirement.
        Only enter when current bar shows strong flow AND the previous two bars
        both show agreement on direction. This filters out isolated spikes and
        requires genuine persistence of the flow signal.
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
        
        # Get previous two bars for persistence check
        prev_bar = history[-2]
        bar_before_prev = history[-3]
        
        if prev_bar.volume <= 0 or bar_before_prev.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        prev_prev_buy_ratio = bar_before_prev.buy_volume / bar_before_prev.volume
        prev_prev_sell_ratio = bar_before_prev.sell_volume / bar_before_prev.volume
        
        # Strong buy signal: current bar has 56%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Three-bar persistence requirement: previous two bars must also show
            # buying pressure (>0.52 buy ratio). This ensures the flow signal is
            # genuinely persistent, not just two marginal bars above threshold.
            if prev_buy_ratio <= self.persistence_threshold:
                return
            if prev_prev_buy_ratio <= self.persistence_threshold:
                return
            
            # Stricter price action confirmation: close must be in upper QUARTER
            # of bar (0.75 threshold). With three-bar confirmation, we can be
            # more selective about entry timing.
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.75:
                    return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal: current bar has 56%+ sell volume
        elif sell_ratio > self.flow_threshold:
            # Three-bar persistence requirement: previous two bars must also show
            # selling pressure (>0.52 sell ratio).
            if prev_sell_ratio <= self.persistence_threshold:
                return
            if prev_prev_sell_ratio <= self.persistence_threshold:
                return
            
            # Stricter price action confirmation: close must be in lower QUARTER
            # of bar (0.25 threshold).
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > 0.25:
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
