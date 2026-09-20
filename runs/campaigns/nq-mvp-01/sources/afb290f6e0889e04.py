"""
Refined mean-reversion with adaptive thresholds and multi-bar confirmation.

Previous trials:
- #0 (continuation): -0.7271
- #1 (mean-reversion fade): -0.6313

This proposal refines the mean-reversion approach with:
1. Adaptive move threshold based on session volatility evolution
2. Multi-bar confirmation: wait for follow-through bars to confirm reversion
3. Tighter entry timing: enter on the bar *after* outsized move, not immediately
4. Better bracket scaling: use recent volatility to set stops/targets
5. Filter out trades early in session (higher noise) and late in session (less time)

The logic: outsized moves do give back (confirmed by #1 beating #0), but entering
immediately is risky. We wait for the next bar to confirm the reversion is starting,
then fade with properly scaled protection. We also adapt our threshold to how
volatile the session is becoming, and avoid low-conviction trades at session edges.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        min_bars_in_session: int = 2,
        max_bars_before_close: int = 3,
        base_move_threshold: float = 1.4,
        volatility_adapt: float = 0.8,
        base_size: int = 1,
        stop_offset: float = 1.2,
        target_offset: float = 0.6,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.min_bars_in_session = min_bars_in_session
        self.max_bars_before_close = max_bars_before_close
        self.base_move_threshold = base_move_threshold
        self.volatility_adapt = volatility_adapt
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        self._session: int | None = None
        self._session_bars_count: int = 0
        self._last_outsized_info: tuple[str, float, float] | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Reset on new session
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_bars_count = 0
            self._last_outsized_info = None
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        self._session_bars_count += 1
        last_bar = history[-1]
        
        # Don't trade if we have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return
        
        # Filter: avoid trading too early in session (high noise)
        if self._session_bars_count < self.min_bars_in_session:
            return
        
        # Filter: avoid trading too close to session end (no time for reversion)
        # Estimate bars left in session: session_end - current_ts
        time_left_ns = ctx.session_end - ctx.ts
        # Approximate: 5m = 300s = 300e9 ns
        bars_left_estimate = time_left_ns / (5 * 60 * 1e9)  # rough for 5m bars
        if bars_left_estimate < self.max_bars_before_close:
            return
        
        # Need enough history to measure typical range
        if len(history) < self.lookback + 1:
            return
        
        # Measure typical range from lookback window (excluding current bar)
        window = history[-(self.lookback + 1):-1]
        if len(window) < self.lookback:
            return
        
        typical_range = sum(b.high - b.low for b in window) / len(window)
        if typical_range <= 0:
            return
        
        # Adaptive threshold: if session is already volatile, use higher threshold
        # to avoid false signals on naturally volatile days
        session_so_far_range = sum(b.high - b.low for b in history)
        session_avg_range = session_so_far_range / len(history) if len(history) > 0 else typical_range
        volatility_ratio = session_avg_range / typical_range if typical_range > 0 else 1.0
        move_threshold = self.base_move_threshold * (1.0 + self.volatility_adapt * (volatility_ratio - 1.0))
        
        last_range = last_bar.high - last_bar.low
        last_move = abs(last_bar.close - last_bar.open)
        
        # Outsized move detection
        is_outsized = (
            last_range > typical_range * move_threshold or
            last_move > typical_range * move_threshold
        )
        
        # Check if we detected an outsized move on the previous bar
        # If so, this bar should show early signs of reversion
        if self._last_outsized_info is not None:
            prev_direction, prev_magnitude, prev_typical = self._last_outsized_info
            
            # Check for reversion confirmation: current bar should move opposite
            if len(history) >= 2:
                prev_bar = history[-2]
                current_bar = history[-1]
                
                # Reversion confirmation: price moving back toward pre-outsized level
                if prev_direction == "up":
                    # Previous was up, now we want to see a down move or at least lower close
                    is_reverting = current_bar.close < prev_bar.close
                else:
                    # Previous was down, now we want to see an up move or at least higher close
                    is_reverting = current_bar.close > prev_bar.close
                
                if is_reverting:
                    # Enter the fade trade on confirmation
                    move_magnitude = prev_magnitude
                    size_multiplier = min(2.0, 1.0 + (move_magnitude / prev_typical) * 0.5)
                    size = max(1, int(self.base_size * size_multiplier))
                    
                    stop_mult = self.stop_offset * max(1.0, move_magnitude / prev_typical)
                    target_mult = self.target_offset * max(0.5, move_magnitude / prev_typical)
                    
                    if prev_direction == "up":
                        # Sell to fade the up move
                        ctx.sell(
                            size=size,
                            stop_offset=max(0.5, stop_mult),
                            target_offset=max(0.25, target_mult),
                            tag="fade-confirmed-up",
                        )
                    else:
                        # Buy to fade the down move
                        ctx.buy(
                            size=size,
                            stop_offset=max(0.5, stop_mult),
                            target_offset=max(0.25, target_mult),
                            tag="fade-confirmed-down",
                        )
            
            # Clear the stored outsized info after attempting trade
            self._last_outsized_info = None
        
        # Store new outsized move for next bar confirmation
        if is_outsized:
            if last_bar.close > last_bar.open:
                direction = "up"
                magnitude = last_bar.close - last_bar.open
            elif last_bar.close < last_bar.open:
                direction = "down"
                magnitude = last_bar.open - last_bar.close
            else:
                direction = None
                magnitude = 0.0
            
            if direction is not None:
                self._last_outsized_info = (direction, magnitude, typical_range)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
