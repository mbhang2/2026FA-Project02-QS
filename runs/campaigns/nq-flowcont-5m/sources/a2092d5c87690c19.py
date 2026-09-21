"""
Flow continuation strategy: testing adaptive entry sizing based on flow strength.

Analysis of prior trials:
- #10/#9: -0.1437, 768 trades (2.0/session) → best so far
- #13: -0.1817, 768 trades (2.0/session) → volatility filter hurt
- #12: -0.2181, 767 trades (2.0/session) → over-filtering hurt
- #8: -0.1829, 768 trades (2.0/session) → baseline

Key insight from trial history:
The best results came from #9/#10 with simple, fixed parameters:
- Flow threshold: 0.56 (56%+)
- Previous bar agreement: 0.52 (52%+)
- Price action: 0.60 threshold
- Bracket: 1.5 stop / 3.5 target
- 2 entries per session

The volatility filter in #13 actually made things worse (-0.1817), suggesting
that trading in quiet periods isn't the problem. The issue is something else.

New hypothesis - the problem might be ENTRY CONVICTION:
We're entering on bars that meet the criteria but have marginal flow strength.
A bar with 56.1% buy volume is technically a "strong buy" but is barely over
the threshold. A bar with 65%+ buy volume is clearly strong.

This iteration tests:
**Size entries based on flow strength conviction, not fixed size.**

Instead of always buying/selling 1 contract when flow > 0.56:
- 56-60% flow: skip (too marginal, barely above threshold)
- 60-65% flow: buy/sell 1 contract
- 65%+ flow: buy/sell 2 contracts (higher conviction = larger size)

This way:
1. We avoid marginal signals (56-60%) that have poor follow-through
2. We size up on stronger signals (65%+) where the edge is real
3. We maintain similar total trade count but with better quality entries
4. The 2 entry/session limit still applies, but we're more selective

The bracket remains 1.5 stop / 3.5 target for all entries.

This is a cleaner approach than the volatility filter because it directly
addresses signal quality rather than trying to proxy it through volatility.
The strongest flow bars should have the best continuation, so let's trade them.

Risk: We might trade slightly less (maybe 600-700 trades instead of 768).
But those trades should have significantly higher edge because we're avoiding
the marginal 56-60% signals that are probably breaking even or worse.

Expected outcome: Better score than #9/#10's -0.1437 because we're filtering
out the weakest signals that don't justify the 0.20 commission cost.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold_low: float = 0.56,
        flow_threshold_medium: float = 0.60,
        flow_threshold_high: float = 0.65,
        prev_agreement_threshold: float = 0.52,
        stop_offset: float = 1.5,
        target_offset: float = 3.5,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold_low: Below this, skip the signal (too marginal)
            flow_threshold_medium: At or above this, size = 1 contract
            flow_threshold_high: At or above this, size = 2 contracts
            prev_agreement_threshold: Previous bar should show agreement (0.52+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.5 points)
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold_low = flow_threshold_low
        self.flow_threshold_medium = flow_threshold_medium
        self.flow_threshold_high = flow_threshold_high
        self.prev_agreement_threshold = prev_agreement_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
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
        Entry logic: flow continuation with adaptive sizing based on conviction.
        Skip marginal signals (56-60%), size 1x on medium signals (60-65%),
        size 2x on strong signals (65%+).
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
        
        # Strong buy signal: current bar has significant buy volume
        if buy_ratio > self.flow_threshold_low:
            # Skip if below medium threshold (too marginal)
            if buy_ratio < self.flow_threshold_medium:
                return
            
            # Previous bar agreement: should show some buying (>0.52) or at least
            # not show strong selling (<=0.55).
            if prev_sell_ratio > 0.55:
                return
            
            # Price action confirmation: close must be in upper 60% of bar
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.60:
                    return
            
            # Determine size based on flow strength conviction
            if buy_ratio >= self.flow_threshold_high:
                size = 2  # Strong signal: 65%+ on one side
            else:
                size = 1  # Medium signal: 60-65% on one side
            
            ctx.buy(
                size=size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal: current bar has significant sell volume
        elif sell_ratio > self.flow_threshold_low:
            # Skip if below medium threshold (too marginal)
            if sell_ratio < self.flow_threshold_medium:
                return
            
            # Previous bar agreement: should show some selling (>0.52) or at least
            # not show strong buying (<=0.55).
            if prev_buy_ratio > 0.55:
                return
            
            # Price action confirmation: close must be in lower 40% of bar
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > 0.40:
                    return
            
            # Determine size based on flow strength conviction
            if sell_ratio >= self.flow_threshold_high:
                size = 2  # Strong signal: 65%+ on one side
            else:
                size = 1  # Medium signal: 60-65% on one side
            
            ctx.sell(
                size=size,
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
