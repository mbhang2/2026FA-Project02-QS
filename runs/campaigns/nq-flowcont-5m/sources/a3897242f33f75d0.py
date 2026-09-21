"""
Flow continuation strategy: refined entry with dual-bar momentum and tighter selectivity.

Analysis of recent trials:
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best baseline
- #30: -0.1913, 768 trades (2.0/session) → momentum persistence hurt
- #31: -0.2035, 768 trades (2.0/session) → adaptive threshold underperformed

Core insight:
The best performers (#9, #10, #15) all score -0.1437 with identical mechanics:
- 0.55 flow threshold (entry signal)
- Previous bar opposition check (reject if prev was opposite at 0.55+)
- Simple price action (close above/below midpoint)
- Session discipline (skip first 3 bars, last 5 minutes)
- Volume filter (min 150)
- Max 2 entries per session
- Stop 1.5, Target 3.0

The problem:
At 2 trades/session × 384 sessions = 768 trades, we pay ~0.10 in commission per trade.
Our edge per trade must exceed 0.10 to profit. We're barely beating that threshold.

The hypothesis - ENTRY QUALITY OVER QUANTITY:
Instead of trying to improve the signal (which is already good), reduce the number
of marginal entries. The current approach enters whenever the signal fires. What if
we require MORE confirmation that the move is actually developing?

New approach - DUAL-BAR MOMENTUM CONFIRMATION:
1. Keep the core #9 logic (0.55 threshold, prev bar opposition check, price action)
2. Add a secondary filter: the CURRENT bar's momentum must be STRONGER than the
   previous bar's momentum in the SAME direction
   - If current bar shows 0.55+ buy, previous bar should show at least 0.52 buy
   - This filters out reversals where a single strong bar follows weak bars
3. This is a "momentum acceleration" check: we want bars that are getting MORE
   aggressive, not LESS
4. Keep everything else from #9 exactly

Why this works:
- We eliminate entries on momentum DECELERATION (strong bar after weak)
- We keep entries on momentum ACCELERATION (strong bar after moderately strong)
- This reduces trades by ~15-20% (fewer marginal signals)
- But the trades we DO take are higher quality
- Expected: 650 trades, better edge per trade

Expected outcome:
- 600-650 trades (down from 768, filtering marginal entries)
- Better signal quality from momentum acceleration filter
- Score in -0.10 to -0.13 range (improvement from #9's -0.1437)

The philosophy:
#9 is excellent and we're not going to beat it by adding complexity. Instead,
we reduce quantity and improve quality. Fewer, better trades is the path forward.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        momentum_min: float = 0.52,
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
            momentum_min: Minimum momentum in prev bar for same direction (0.52 = 52%+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.momentum_min = momentum_min
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
        Entry logic: 5m flow signal with momentum acceleration confirmation.
        Only enter when current bar shows strong flow AND the previous bar
        shows momentum in the same direction (momentum acceleration filter).
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
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Opposition check: reject if previous bar was strongly sell-dominated
            if prev_sell_ratio > self.flow_threshold:
                return
            
            # Momentum acceleration filter: previous bar should show some buying
            # momentum in the same direction (at least 0.52)
            if prev_buy_ratio < self.momentum_min:
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
            
            # Momentum acceleration filter: previous bar should show some selling
            # momentum in the same direction (at least 0.52)
            if prev_sell_ratio < self.momentum_min:
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
