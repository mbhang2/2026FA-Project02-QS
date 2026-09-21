"""
Flow continuation strategy: Refined entry timing with momentum confirmation.

Analysis of 46 trials shows best score is -0.1370 (trial #43).

Key observations:
1. Trial #43: -0.1370 (close_position=0.67, target=4.0, 768 trades) - BEST
2. Trial #45: -0.1692 (close_position=0.75, target=5.0, 768 trades)
3. Trial #46: -0.2743 (volume normalization, 768 trades)

The pattern is clear: Trial #43's parameters are optimal. But we're still negative.

Critical realization: The issue might not be WHAT we filter but WHEN we enter.

Current approach enters on the bar AFTER flow occurs. But what if we're entering
too late - after the initial spike has already happened? Or entering on bars where
the flow was exhausted?

New hypothesis - MOMENTUM CONTINUATION CHECK:
Instead of just checking if the current bar has strong flow, check if the NEXT
bar is likely to continue. We can't see the future, but we can use:
1. The CLOSE POSITION within the bar (already doing this)
2. The RELATIONSHIP between current and previous bar flow

If a bar has 55%+ buy flow AND the previous bar also showed buying pressure
(even if not 55%+), that's a SUSTAINED move, not a one-bar spike.

This is different from opposition_check: opposition_check REJECTS if prev bar
opposed. This new check REQUIRES some continuation signal in the previous bar.

New approach - REQUIRE FLOW MOMENTUM (2-BAR CONFIRMATION):
1. Keep flow_threshold at 0.55 (proven)
2. Add momentum check: previous bar must show SOME buying (buy_ratio > 0.45)
   for buy signals, SOME selling (sell_ratio > 0.45) for sell signals
3. This is SOFTER than opposition check but STRONGER than no check
4. Keep close_position_threshold at 0.67 (proven from trial #43)
5. Keep target=4.0, stop=1.5 (proven from trial #43)
6. Keep opposition check (but now it's checking for STRONG opposition at 0.55)
7. Expected: 500-600 trades (filtered for momentum)
8. Score improvement to -0.12 to -0.10 range

Why this works:
- Trial #43 was best; we're keeping its proven parameters
- Adding momentum check filters for SUSTAINED moves, not one-bar spikes
- A bar with 55% buy flow preceded by 45%+ buy is a real continuation
- A bar with 55% buy flow preceded by 65% sell is fake (opposition check catches this)
- This sits between opposition_check (too strict) and no check
- Natural market microstructure: real moves show momentum, spikes reverse

Why we haven't tried this:
- We've been tweaking entry thresholds and targets, not momentum
- This is a subtle improvement on the proven trial #43 approach
- Requires checking TWO bars, not just current bar's properties

Implementation:
1. Keep all proven parameters from trial #43
2. Add momentum_threshold: 0.45 (softer than flow_threshold 0.55)
3. For buy: require buy_ratio > 0.55 AND prev_buy_ratio > 0.45
4. For sell: require sell_ratio > 0.55 AND prev_sell_ratio > 0.45
5. Keep opposition_check (prev_sell_ratio <= 0.55 for buys)
6. Keep close_position_threshold at 0.67
7. Keep target=4.0, stop=1.5

Expected outcome:
- 500-600 trades (down from 768, filtered for momentum)
- Much higher conviction entries (must show 2-bar flow pattern)
- Better win rate from filtering spikes
- Score: -0.12 to -0.10 (improvement from -0.1370)
- More robust signal that survives fill slippage better
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        momentum_threshold: float = 0.45,
        opposition_threshold: float = 0.55,
        stop_offset: float = 1.5,
        target_offset: float = 4.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
        close_position_threshold: float = 0.67,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side (0.55 = 55%+)
            momentum_threshold: Previous bar must show some momentum (0.45 = 45%+)
            opposition_threshold: Reject if prev bar opposed at this level (0.55)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (4.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
            close_position_threshold: Close must be in outer third (0.67)
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
        self.close_position_threshold = close_position_threshold
        
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
        Requires current bar strong flow AND previous bar showing momentum.
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
        
        # Get previous bar for momentum and opposition checks
        prev_bar = history[-2]
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Price action confirmation: close must be in outer third of range
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            return
        
        close_position = (last_bar.close - last_bar.low) / bar_range
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Opposition check: previous bar shouldn't show strong selling
            if prev_sell_ratio > self.opposition_threshold:
                return
            
            # NEW: Momentum check - previous bar must show some buying momentum
            # Not as strict as flow_threshold, but must show direction
            if prev_buy_ratio < self.momentum_threshold:
                return
            
            # Stricter price action: close must be in UPPER THIRD of range
            if close_position < self.close_position_threshold:
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
            # Opposition check: previous bar shouldn't show strong buying
            if prev_buy_ratio > self.opposition_threshold:
                return
            
            # NEW: Momentum check - previous bar must show some selling momentum
            if prev_sell_ratio < self.momentum_threshold:
                return
            
            # Stricter price action: close must be in LOWER THIRD of range
            if close_position > (1.0 - self.close_position_threshold):
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
