"""
Flow continuation strategy: testing reduced entry frequency with stricter confirmation.

Analysis of prior trials:
- #17: -0.2084, 768 trades (2.0/session) → asymmetric thresholds didn't help
- #9/#10: -0.1437, 768 trades (2.0/session) → best so far
- #16: -0.2185, 768 trades (2.0/session) → multi-bar momentum was too strict
- #8: -0.1829, 768 trades (2.0/session) → baseline

Key observation from all trials:
We consistently get 768 trades (2.0 per session), and the score hovers around -0.14 to -0.21.
The best result (#9/#10 at -0.1437) used simple, clean logic without over-engineering.

The core issue: At 768 trades with 0.20 commission per round turn, we're paying ~76.8
index points in commission. Our gross P&L before commission needs to be >76.8 just to break even.
At -0.1437 with 768 trades, our gross is ~-0.0563 per trade, meaning we're losing on the edge itself.

New hypothesis - ENTRY QUALITY OVER QUANTITY:
What if we're entering too often? The constraint is 2 entries per session, but maybe
we should be MORE selective. What if we only enter when:
1. The flow signal is VERY strong (0.57+ instead of 0.56)
2. AND the bar has above-average volume (not just >150, but >250)
3. AND we limit to 1 entry per session instead of 2

This would give us ~384 trades total instead of 768 - cutting entries in half.
If we can improve edge per trade enough (from -0.0563 to -0.0100 or better),
we'd see: 384 * (-0.0100) = -0.0384 score, a big improvement.

The logic:
- Higher flow threshold: 0.57 (very strong conviction)
- Higher volume threshold: 250 (avoid thin bars entirely)
- Lower entry frequency: 1 per session (be selective)
- Keep everything else that worked: price action at 0.60, bracket 1.5/3.5, timing discipline

Why this might work:
- We're trading the STRONGEST signals only
- We avoid microstructure noise in thin bars
- We reduce whipsaw frequency
- Commission impact is cut in half (384 trades vs 768)
- Each trade needs less edge to be profitable

Risk: If we miss the bulk of the edge by being too selective, we could score worse.
But the trial history suggests our edge is small and inconsistent - being selective
about WHICH trades we take is better than taking every borderline signal.

This is a fundamental shift from "maximize trade count" to "maximize edge per trade".
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.57,
        prev_threshold: float = 0.55,
        close_threshold: float = 0.60,
        stop_offset: float = 1.5,
        target_offset: float = 3.5,
        size: int = 1,
        min_volume: int = 250,
        max_entries_per_session: int = 1,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Flow signal requires >57% on one side (stricter)
            prev_threshold: Previous bar rejection threshold (0.55)
            close_threshold: Close position in range threshold (0.60)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.5 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio (increased to 250)
            max_entries_per_session: Maximum entries per session (reduced to 1)
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
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
        Entry logic: high-conviction flow signals with selective entry.
        Only enter on very strong signals (0.57+) with above-average volume.
        Maximum 1 entry per session to focus on best opportunities.
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
        
        # Limit entries per session (now 1 instead of 2)
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
        
        # Skip low-volume bars (higher threshold: 250 instead of 150)
        # This ensures we only trade bars with solid participation
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
        
        # Buy signal: very strong flow (57%+ buy volume)
        if buy_ratio > self.flow_threshold:
            # Previous bar check: only reject if prev was strongly sell-dominated
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
        
        # Sell signal: very strong flow (57%+ sell volume)
        elif sell_ratio > self.flow_threshold:
            # Previous bar check: only reject if prev was strongly buy-dominated
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
