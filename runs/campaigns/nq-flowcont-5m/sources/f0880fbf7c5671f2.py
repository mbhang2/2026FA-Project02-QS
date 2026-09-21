"""
Flow continuation strategy: testing asymmetric entry thresholds with directional bias.

Analysis of prior trials:
- #16: -0.2185, 768 trades (2.0/session) → multi-bar momentum filter was too strict
- #15: -0.2181, 767 trades (2.0/session) → close threshold 0.67 was too tight
- #9/#10: -0.1437, 768 trades (2.0/session) → best so far
- #8: -0.1829, 768 trades (2.0/session) → baseline

Key insight from #16's regression:
Requiring BOTH current AND previous bars to exceed 0.52 was too restrictive.
We went from -0.1437 to -0.2185, suggesting we filtered out too many valid trades.

The best result (#9/#10 at -0.1437) used:
- Current bar: 0.56 threshold
- Previous bar: only required NOT to be opposite at 0.55+ (softer constraint)
- Price action: 0.60 close threshold
- Bracket: 1.5 stop / 3.5 target
- 2 entries per session

New hypothesis - the issue might be ASYMMETRIC THRESHOLDS:
What if buys and sells have different optimal thresholds?
Market microstructure is often asymmetric - buying pressure might continue differently
than selling pressure. What if we need 0.55 for sells but 0.57 for buys?

Also, what if the previous bar check should be WEAKER for one direction?
For example:
- For buys: require current > 0.56 AND previous NOT sell-dominated (< 0.55)
- For sells: require current > 0.56 AND previous NOT buy-dominated (< 0.55)

This is what #9 had, but maybe we can improve by tuning the thresholds separately.

Testing strategy:
1. Raise buy threshold to 0.57 (slightly stricter on buys)
2. Keep sell threshold at 0.56 (easier on sells, as they might be more reliable)
3. Previous bar check: soften from 0.55 to 0.54 (allow more agreement before rejecting)
4. Price action: keep 0.60 (proven)
5. Bracket: keep 1.5 stop / 3.5 target
6. Keep all session discipline (skip first 3 bars, last 5 minutes)

Rationale:
- Buys might be noisier (retail buying pressure), so raise threshold
- Sells might be cleaner (institutional selling), so keep lower threshold
- Previous bar: 0.54 instead of 0.55 means "only reject if prev was REALLY opposite"
- This asymmetry might better match market structure

Expected outcome:
- Fewer buy signals (0.57 is stricter), more sell signals
- Better directional balance
- Potentially better edge per trade
- Score could improve toward -0.1437 or better

This is a subtle refinement of #9, not a major structural change.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        buy_threshold: float = 0.57,
        sell_threshold: float = 0.56,
        prev_threshold: float = 0.54,
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
            buy_threshold: Buy signal requires >57% buy volume (stricter)
            sell_threshold: Sell signal requires >56% sell volume (looser)
            prev_threshold: Previous bar rejection threshold (0.54 = softer)
            close_threshold: Close position in range threshold (0.60)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.5 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.prev_threshold = prev_threshold
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
        Entry logic: asymmetric flow thresholds with softer previous bar check.
        Buys require 57%, sells require 56%. Previous bar rejection at 0.54.
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
        
        # Buy signal: current bar has >57% buy volume (stricter than sell)
        if buy_ratio > self.buy_threshold:
            # Previous bar rejection: only reject if prev was REALLY sell-dominated
            # (softer threshold at 0.54 instead of 0.55)
            if prev_sell_ratio > self.prev_threshold:
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
        
        # Sell signal: current bar has >56% sell volume (easier than buy)
        elif sell_ratio > self.sell_threshold:
            # Previous bar rejection: only reject if prev was REALLY buy-dominated
            # (softer threshold at 0.54 instead of 0.55)
            if prev_buy_ratio > self.prev_threshold:
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
