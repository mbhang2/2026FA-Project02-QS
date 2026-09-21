"""
Flow continuation strategy: testing entry timing and bar persistence.

Analysis of prior trials:
- #9: -0.1437, 768 trades (2.0/session) → best so far
- #8: -0.1829, 768 trades (2.0/session) → previous best
- #6: -0.2427, 768 trades (2.0/session) → baseline

Key observation from #9's improvement over #8:
The tighter price action confirmation (upper/lower THIRD instead of HALF)
and the higher flow threshold (0.56 vs 0.55) helped, but we're still
slightly negative (-0.1437 vs -0.1829).

The remaining problem: we're still paying 0.20 per round turn in commission
on ~2 trades per session. We need either:
1. Better entry timing (enter only on the highest conviction signals)
2. Fewer but higher-quality trades
3. Better exit timing (hold winners longer, cut losers faster)

New hypothesis for this iteration:
**Bar persistence matters more than we thought.** #9 enters on ANY bar that
meets the flow + price action criteria. But what if we're entering on isolated
bars that reverse? The campaign says "aggressive flow continues" - maybe we
should only enter when we see TWO consecutive bars showing the same directional
flow, not just one.

This iteration tests:
1. **Bar persistence requirement**: Require current bar AND previous bar to both
   show agreement on direction (both >0.52 on same side). This filters out
   whipsaws and isolated bars.
   
2. **Slightly relaxed price action**: Since we're now requiring TWO bars of
   agreement, we can be slightly less strict on the close position. Use 0.60
   threshold instead of 0.67 - still upper/lower THIRD region but a bit more
   forgiving.
   
3. **Keep everything else from #9**: 0.56 flow threshold, 1.5/3.5 bracket,
   session skipping, volume filter.

The logic: If we see a 0.56+ buy bar, we check the previous bar. If the
previous bar ALSO shows buying pressure (>0.52), that's a signal that buying
is persistent, not a one-bar spike. Same for sells.

This should:
- Reduce whipsaws (fewer false entries)
- Improve edge per trade
- Potentially trade less but with higher conviction
- Better align with "flow continues" hypothesis

Risk: We might miss some valid continuations. But the commission math suggests
we need to be MORE selective, not less.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.56,
        persistence_threshold: float = 0.52,
        stop_offset: float = 1.5,
        target_offset: float = 3.5,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side (0.56 = 56%+)
            persistence_threshold: Previous bar should show agreement (0.52+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.5 points)
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
        Entry logic: flow continuation with bar persistence requirement.
        Only enter when BOTH current and previous bars show agreement on direction.
        This filters out isolated spikes and requires persistence of the flow signal.
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
        
        # Get previous bar for persistence check
        prev_bar = history[-2]
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Strong buy signal: current bar has 56%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Persistence requirement: previous bar must also show buying
            # (>0.52 buy ratio). This ensures the flow signal is persistent,
            # not a one-bar spike.
            if prev_buy_ratio <= self.persistence_threshold:
                return
            
            # Price action confirmation: close must be in upper 40% of bar
            # (slightly more relaxed than #9's 0.67 since we now have
            # persistence requirement)
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
        
        # Strong sell signal: current bar has 56%+ sell volume
        elif sell_ratio > self.flow_threshold:
            # Persistence requirement: previous bar must also show selling
            # (>0.52 sell ratio).
            if prev_sell_ratio <= self.persistence_threshold:
                return
            
            # Price action confirmation: close must be in lower 40% of bar
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
