"""
Flow continuation strategy: refined two-bar persistence with fixed thresholds.

Analysis of prior trials:
- #10: -0.1437, 768 trades (2.0/session) → tied for best
- #9: -0.1437, 768 trades (2.0/session) → tied for best
- #11: -0.2347, 381 trades (0.99/session) → too selective
- #8: -0.1829, 768 trades (2.0/session) → previous baseline

Key insight from the trial history:
The adaptive threshold approach in #12 was too complex. The mypy error on
_baseline_buy_ratios revealed the issue: we're overthinking this. The best
scores came from #9-#10 with simple, fixed thresholds and two-bar persistence.

What's clear from the data:
1. Two-bar persistence (current >0.56, previous >0.52) works well
2. Price action at 0.60 threshold is solid
3. 1.5/3.5 bracket with 2 entries/session gives ~768 trades
4. We're at -0.1437 which is still negative but the best we have

The remaining edge problem: we need to be MORE selective about which bars
we enter on, not less. The issue isn't the thresholds - they're calibrated
right. The issue is that we're entering on bars that look good but don't
follow through.

New hypothesis for this iteration:
**Require stronger previous bar agreement AND tighter close positioning.**

Instead of prev_buy_ratio > 0.52 (just barely above 50%), require >0.54.
This means the previous bar needs to show more convincing buying, not just
any buying at all.

Additionally, for the close position, be stricter:
- Buy entries: close must be in upper 50% of bar (0.50), not 0.60
- Sell entries: close must be in lower 50% of bar (0.50), not 0.40

This combination:
- Keeps two-bar persistence (proven to work)
- Raises the bar for "agreement" (0.54 vs 0.52)
- Tightens close positioning (0.50 vs 0.60/0.40)
- Should reduce false entries while keeping trade count similar

The logic: We're looking for bars where:
1. Current bar shows strong directional flow (>0.56)
2. Previous bar shows CONVINCING directional agreement (>0.54, not just >0.52)
3. Close is right in the middle or better (0.50+), not just anywhere in upper half

This filters out marginal signals while keeping the core two-bar persistence
that has proven effective.

Risk: We might trade slightly less (maybe 700-750 trades instead of 768).
But those trades should have higher conviction and better edge.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.56,
        persistence_threshold: float = 0.54,
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
            persistence_threshold: Previous bar should show agreement (0.54+)
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
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: flow continuation with stricter two-bar persistence.
        Require both bars to show convincing directional agreement, not marginal.
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
            # Stricter persistence requirement: previous bar must show convincing
            # buying (>0.54, not just >0.52). This ensures real agreement, not
            # marginal buying.
            if prev_buy_ratio <= self.persistence_threshold:
                return
            
            # Tighter price action confirmation: close must be at or above midpoint
            # of bar (0.50 threshold). This filters out bars that close high only
            # on a narrow range.
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.50:
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
            # Stricter persistence requirement: previous bar must show convincing
            # selling (>0.54 sell ratio).
            if prev_sell_ratio <= self.persistence_threshold:
                return
            
            # Tighter price action confirmation: close must be at or below midpoint
            # of bar (0.50 threshold).
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > 0.50:
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
