"""
Flow continuation strategy: testing multi-bar flow persistence with momentum confirmation.

Analysis of prior trials:
- #15: -0.2181, 767 trades (2.0/session) → tight close threshold hurt
- #9/#10: -0.1437, 768 trades (2.0/session) → best so far
- #14: -0.3243, 926 trades (2.4/session) → adaptive sizing was bad
- #8: -0.1829, 768 trades (2.0/session) → baseline

Key insight from #15's regression:
Tightening the close threshold from 0.60 to 0.67 made things worse (-0.2181 vs -0.1437).
This suggests we were filtering out too many valid signals. The sweet spot was 0.60.

The real pattern from the trial history:
- Simple, fixed parameters work best
- #9/#10 at -0.1437 is the best we've achieved
- Modifications that add complexity tend to hurt

But there's still an edge to find. At -0.1437 with 768 trades:
- Commission cost: 0.20 per round turn
- Total commission: ~153.6 index points
- Gross P&L before commission: ~0.0563 per trade
- We're actually profitable before commission!

The issue: our edge is small and inconsistent. Some trades are winners, some losers.

New hypothesis - the problem might be ENTRY QUALITY based on FLOW MOMENTUM:
What if we're entering on bars where flow is strong but momentum is fading?
A bar with 56% buy volume might be the END of a buying wave, not the beginning.

This iteration tests:
**Multi-bar flow confirmation: require current AND previous bar to show same-side flow.**

Instead of just checking "previous bar doesn't contradict us":
1. Current bar has 56%+ on one side (buy or sell)
2. Previous bar also has 52%+ on the SAME side (showing momentum, not just lack of opposition)
3. Price action: close in upper/lower 60% (back to the proven 0.60)
4. Only then enter

This filters out:
- Bars where flow just turned (current bar is strong but previous was weak/opposite)
- Mean-reversion setups
- Whipsaws from flow reversals

The logic:
- Buy only if: current buy_ratio > 0.56 AND previous buy_ratio > 0.52
- Sell only if: current sell_ratio > 0.56 AND previous sell_ratio > 0.52

This is stricter than #9/#10 (which only required prev NOT be opposite at 0.55+).
It requires actual agreement, not just lack of opposition.

Expected outcome:
- Fewer trades (maybe 500-600 instead of 768)
- Higher edge per trade (we're catching momentum, not single-bar spikes)
- Better score than -0.1437 because we're filtering for persistence

Risk: If we miss too many valid trades, total score could worsen. But the
hypothesis is that most of our losing trades come from entering on bars that
are about to reverse, and this filter directly addresses that.

Parameters:
- Flow threshold current: 0.56
- Flow threshold previous: 0.52 (now REQUIRED, not just "not opposite")
- Price action: 0.60 (back to proven level)
- Bracket: 1.5 stop / 3.5 target
- Fixed size: 1 contract
- 2 entries per session
- 3 bar skip at session start
- 5 minute skip at session end
- 150 minimum volume
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold_current: float = 0.56,
        flow_threshold_previous: float = 0.52,
        close_threshold: float = 0.60,
        stop_offset: float = 1.5,
        target_offset: float = 3.5,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold_current: Current bar must have 56%+ on one side
            flow_threshold_previous: Previous bar must have 52%+ on SAME side
            close_threshold: Close position in range threshold (0.60 = upper/lower 60%)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.5 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold_current = flow_threshold_current
        self.flow_threshold_previous = flow_threshold_previous
        self.close_threshold = close_threshold
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
        Entry logic: flow continuation with multi-bar momentum confirmation.
        Require BOTH current and previous bars to show flow on the same side.
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
        
        # Get previous bar for momentum confirmation
        prev_bar = history[-2]
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Strong buy signal with momentum:
        # Current bar has 56%+ buy AND previous bar has 52%+ buy (both sides agree)
        if buy_ratio > self.flow_threshold_current:
            # Previous bar MUST show buying momentum (52%+), not just lack of selling
            if prev_buy_ratio <= self.flow_threshold_previous:
                return
            
            # Price action confirmation: close must be in upper 60% of bar
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < self.close_threshold:
                    return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal with momentum:
        # Current bar has 56%+ sell AND previous bar has 52%+ sell (both sides agree)
        elif sell_ratio > self.flow_threshold_current:
            # Previous bar MUST show selling momentum (52%+), not just lack of buying
            if prev_sell_ratio <= self.flow_threshold_previous:
                return
            
            # Price action confirmation: close must be in lower 40% of bar
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > (1.0 - self.close_threshold):
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
