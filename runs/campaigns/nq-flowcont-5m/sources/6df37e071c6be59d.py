"""
Flow continuation strategy: temporal clustering with multi-bar signal persistence.

Analysis of best performers:
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best baseline
- #36: -0.1495, 768 trades → volume persistence added value
- #37: -0.1865, 768 trades → asymmetric targets hurt (over-complicated)

Key insight from trial history:
The best scores consistently cluster around -0.1437 with 768 trades and simple
logic. Trials that added complexity (#37 with asymmetric targets, #23-#28 with
various enhancements) all underperformed the simple baseline.

The pattern: -0.1437 is a hard ceiling. We're not beating it by adding more
filters or complexity. The issue is that we're entering on REAL signals, but
exiting at the wrong time or with the wrong sizing.

Looking deeper at the problem:
1. Commission is 0.20 per round turn (0.10 per side)
2. At 768 trades over 384 sessions = 2.0 trades/session
3. Average loss of -0.1437 = -0.1437 index points per trade
4. This is roughly break-even after commission, with some trades profitable

The real issue: we're not filtering WHEN to trade, we're accepting too many
signals that are right directionally but wrong on TIMING.

New hypothesis - TEMPORAL CLUSTERING:
Instead of taking every 0.55+ signal, cluster signals in time:
1. When we see a strong buy signal (0.55+), check if the NEXT bar (if we wait
   one bar) also shows buying pressure (0.50+).
2. Only enter on the SECOND confirmation bar, not the first.
3. This naturally filters out one-bar spikes that reverse.
4. Trade count drops from 768 to ~500-600, but edge per trade improves.
5. The cost: we miss some moves, but we avoid whipsaws.

Why this works:
- A one-bar spike that reverses costs us 0.30 (0.20 commission + 0.10 slippage)
- A confirmed two-bar pattern that moves 2 points makes 1.70 profit
- Waiting one bar to confirm is a 1.8 point swing in expected value
- Expected: 500-600 trades, -0.05 to -0.08 score (MUCH better)

Why we haven't tried this:
- We can't see the next bar in on_bars
- But we CAN keep state: if bar[t] shows buy signal, wait for bar[t+1]
- Only enter on bar[t+1] if it also shows buying pressure
- This is exactly what a real trader does: wait for confirmation

Implementation:
1. Keep a "pending_signal" on self: (direction, strength, bar_index)
2. When we see a 0.55+ signal, store it but DON'T enter
3. On the next bar, check if it shows agreement (0.50+ on same side)
4. If yes, enter. If no, clear the pending signal.
5. This adds one bar of latency but dramatically improves signal quality.

Expected outcome:
- 500-600 trades (vs 768 baseline)
- Better edge per trade from confirmation
- Score in -0.05 to -0.08 range (vs -0.1437 baseline)
- Fewer whipsaws, more conviction entries
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        confirmation_threshold: float = 0.50,
        opposition_threshold: float = 0.55,
        volume_ratio_min: float = 0.80,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio for initial signal detection (0.55)
            confirmation_threshold: Minimum ratio for confirmation bar (0.50)
            opposition_threshold: Reject if prev bar shows opposite at this level
            volume_ratio_min: Current bar volume >= this ratio of previous bar
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.confirmation_threshold = confirmation_threshold
        self.opposition_threshold = opposition_threshold
        self.volume_ratio_min = volume_ratio_min
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0
        
        # Pending signal waiting for confirmation
        # Format: (direction, signal_strength) where direction is 'buy' or 'sell'
        self._pending_signal: tuple[str, float] | None = None

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: two-bar confirmation pattern.
        Bar 1: detect strong flow signal (0.55+)
        Bar 2: confirm with weaker agreement (0.50+)
        Only enter on bar 2 if confirmation holds.
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
            self._pending_signal = None
        
        self._bars_in_session += 1
        
        # One position at a time
        if ctx.position or ctx.working_order_ids:
            # Clear pending signal if we're in a position
            self._pending_signal = None
            return
        
        # Limit entries per session
        if self._entries_this_session >= self.max_entries_per_session:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 2:
            return
        
        last_bar = history[-1]  # Bar that just closed
        
        # Skip first 3 bars of session
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:
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
        
        # Get previous bar for opposition and volume checks
        prev_bar = history[-2]
        
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Volume persistence check
        volume_ratio = last_bar.volume / prev_bar.volume
        if volume_ratio < self.volume_ratio_min:
            return
        
        # Price action confirmation: close on correct side of midpoint
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            return
        
        close_position = (last_bar.close - last_bar.low) / bar_range
        
        # Check if we have a pending signal to confirm
        if self._pending_signal is not None:
            direction, signal_strength = self._pending_signal
            
            if direction == "buy":
                # Confirm if this bar shows buying pressure (0.50+)
                if buy_ratio > self.confirmation_threshold and close_position >= 0.5:
                    # Opposition check on current bar
                    if prev_sell_ratio <= self.opposition_threshold:
                        ctx.buy(
                            size=self.size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="flow-long-confirmed",
                        )
                        self._entries_this_session += 1
                        self._pending_signal = None
                        return
            
            elif direction == "sell":
                # Confirm if this bar shows selling pressure (0.50+)
                if sell_ratio > self.confirmation_threshold and close_position <= 0.5:
                    # Opposition check on current bar
                    if prev_buy_ratio <= self.opposition_threshold:
                        ctx.sell(
                            size=self.size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="flow-short-confirmed",
                        )
                        self._entries_this_session += 1
                        self._pending_signal = None
                        return
            
            # If confirmation failed, clear the pending signal
            self._pending_signal = None
        
        # Look for new signals to set as pending
        if buy_ratio > self.flow_threshold:
            # Opposition check
            if prev_sell_ratio <= self.opposition_threshold:
                # Price action check
                if close_position >= 0.5:
                    # Set as pending, wait for next bar confirmation
                    self._pending_signal = ("buy", buy_ratio)
        
        elif sell_ratio > self.flow_threshold:
            # Opposition check
            if prev_buy_ratio <= self.opposition_threshold:
                # Price action check
                if close_position <= 0.5:
                    # Set as pending, wait for next bar confirmation
                    self._pending_signal = ("sell", sell_ratio)

    def on_fill(self, ctx) -> None:
        """Position filled. Bracket handles the exit."""
        pass

    def on_lifecycle(self, ctx) -> None:
        """Track lifecycle events."""
        pass
