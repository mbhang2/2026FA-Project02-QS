"""
Flow continuation strategy: Refined entry timing with intra-session momentum.

Analysis of 40 trials reveals:
- Best score: -0.1437 (trials #9, #15) with 768 trades, 0.55 threshold
- Multi-timeframe confirmation (#40): -0.1996 (hourly context hurt more than helped)
- Volatility adaptation (#39): -0.1996 (added complexity without edge)
- Core insight: The 0.55 flow signal is robust; the problem is WHEN we enter

Key observation from trial history:
The -0.1437 score is remarkably stable across many parameter variations when
trade count stays at ~768 (2.0/session). This suggests:
1. The signal itself is solid
2. Commission (0.20 per round turn) is the binding constraint
3. We need to improve ENTRY TIMING, not signal quality

New hypothesis - INTRA-SESSION MOMENTUM CONFIRMATION:
Instead of entering on every 0.55+ signal, filter by intra-session momentum:
1. Keep the core 5m bar 0.55 flow threshold (proven)
2. Track cumulative delta (buy_volume - sell_volume) over the session
3. Only enter BUY signals when session cumulative delta is positive
   (market has been buying more than selling overall)
4. Only enter SELL signals when session cumulative delta is negative
   (market has been selling more than buying overall)
5. This filters out counter-trend entries that are likely to whipsaw
6. Expected: 600-700 trades (down from 768), but much better win rate
7. Score improvement to -0.10 to -0.12 range

Why this works:
- A 5m buy signal in a session with negative cumulative delta is fighting the trend
- A 5m buy signal in a session with positive cumulative delta aligns with momentum
- Session-level momentum is the "gravity" that determines if local signals work
- This is NOT lookahead: cumulative delta is computed from bars we've seen
- Reduces noise trades without over-filtering

Why we haven't tried this:
- Requires tracking session-level state (cumulative delta)
- But it's simpler than multi-timeframe and more targeted than volatility

Implementation:
1. In on_bars, accumulate buy_volume - sell_volume for the session
2. Only enter on 5m signal if session momentum aligns
3. Keep all other logic from trial #9 (opposition check, volume filter, timing)
4. Expected trade count: 650-700 (natural reduction from alignment filter)

Expected outcome:
- 650-700 trades (down from 768, but higher conviction)
- Better win rate from momentum alignment
- Score: -0.10 to -0.12 (improvement from -0.1437)
- More robust filtering without over-complication
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        opposition_threshold: float = 0.55,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
        momentum_threshold: float = 0.0,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side (0.55)
            opposition_threshold: Reject if prev bar opposed at this level (0.55)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
            momentum_threshold: Session delta threshold for entry (0.0 = any)
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.opposition_threshold = opposition_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        self.momentum_threshold = momentum_threshold
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0
        
        # Track session-level cumulative delta for momentum
        self._session_cumulative_delta: float = 0.0

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal filtered by session momentum.
        Only enter when signal aligns with intra-session buying/selling pressure.
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
            self._session_cumulative_delta = 0.0
        
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
        
        # Accumulate session-level delta for momentum tracking
        self._session_cumulative_delta += (last_bar.buy_volume - last_bar.sell_volume)
        
        # Skip first 3 bars of session (cash open microstructure)
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes
            return
        
        # Skip low-volume bars
        if last_bar.volume < self.min_volume:
            return
        
        # Measure flow ratios
        buy_vol = last_bar.buy_volume
        sell_vol = last_bar.sell_volume
        total_vol = last_bar.volume
        
        if total_vol <= 0:
            return
        
        buy_ratio = buy_vol / total_vol
        sell_ratio = sell_vol / total_vol
        
        # Get previous bar for opposition check
        prev_bar = history[-2]
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Price action confirmation: close on correct side of midpoint
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            return
        
        close_position = (last_bar.close - last_bar.low) / bar_range
        
        # Strong buy signal on 5m
        if buy_ratio > self.flow_threshold:
            # Opposition check: previous bar shouldn't be strongly sold
            if prev_sell_ratio > self.opposition_threshold:
                return
            
            # Momentum filter: session cumulative delta should be positive
            # (market has been buying more overall)
            if self._session_cumulative_delta <= self.momentum_threshold:
                return
            
            # Price action confirmation: close above midpoint
            if close_position < 0.5:
                return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal on 5m
        elif sell_ratio > self.flow_threshold:
            # Opposition check: previous bar shouldn't be strongly bought
            if prev_buy_ratio > self.opposition_threshold:
                return
            
            # Momentum filter: session cumulative delta should be negative
            # (market has been selling more overall)
            if self._session_cumulative_delta >= -self.momentum_threshold:
                return
            
            # Price action confirmation: close below midpoint
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
