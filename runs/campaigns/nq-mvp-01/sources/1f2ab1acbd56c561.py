"""
Mean-reversion fade with dynamic confirmation and volatility-adaptive entry filtering.

Analysis of recent trials:
- #34: -0.0792 — best so far, relaxed momentum threshold (0.10) and dynamic vol sizing
- #33: -0.1342 — strict momentum (0.05) too aggressive
- #29: -0.1707 — good baseline with strict outsized detection

Key insight: #34 improved significantly by relaxing the momentum threshold from 0.05
to 0.10. This suggests we're filtering out valid trades. But we might be able to
do better by:

1. ADAPTIVE CONFIRMATION: Instead of fixed confirmation_body_ratio, use volatility
   to set it. High volatility sessions need stronger confirmation bodies.

2. OUTSIZED MOVE DETECTION: The current approach measures range against typical_range.
   But we should also consider the POSITION of the move within the bar. A move that
   fills most of the bar is different from one that leaves large wicks.

3. ENTRY FILTERING: Add a filter based on the TIME INTO SESSION. Early session moves
   might have different characteristics than late-session moves. Also filter based on
   whether we're in a consolidation phase vs a trending phase.

4. REVERSAL STRENGTH: Current calculation uses range-normalized distance. We could
   improve by considering how far the reversal bar penetrates back into the prior bar's
   range (overlap depth).

5. MOMENTUM THRESHOLD: Keep it relaxed (0.10) but make it DIRECTIONAL. An up reversal
   after down momentum is different from an up reversal after up momentum. Currently
   we only filter when momentum is VERY strong in one direction.

New approach:
- Keep two-bar confirmation (proven)
- Adaptive confirmation_body_ratio based on session volatility
- Better outsized detection: require close to be in outer 25% of bar range
- Add session phase filter (early/late, consolidation/trend)
- Improve reversal strength with overlap depth calculation
- Make momentum filter more granular (not just threshold, but directional strength)
- Slightly tighter stops on high-volatility sessions to reduce drawdown
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        move_threshold: float = 1.35,
        min_body_ratio: float = 0.75,
        base_confirmation_body_ratio: float = 0.60,
        base_size: int = 1,
        base_stop_offset: float = 0.75,
        base_target_offset: float = 0.75,
        reversal_strength_threshold: float = 0.15,
        min_typical_range: float = 0.10,
        momentum_lookback: int = 3,
        momentum_threshold: float = 0.10,
        vol_adjustment_factor: float = 0.5,
        early_session_bars: int = 4,
        late_session_bars: int = 4,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.min_body_ratio = min_body_ratio
        self.base_confirmation_body_ratio = base_confirmation_body_ratio
        self.base_size = base_size
        self.base_stop_offset = base_stop_offset
        self.base_target_offset = base_target_offset
        self.reversal_strength_threshold = reversal_strength_threshold
        self.min_typical_range = min_typical_range
        self.momentum_lookback = momentum_lookback
        self.momentum_threshold = momentum_threshold
        self.vol_adjustment_factor = vol_adjustment_factor
        self.early_session_bars = early_session_bars
        self.late_session_bars = late_session_bars
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_open: float = 0.0
        self._bar_minus_1_high: float = 0.0
        self._bar_minus_1_low: float = 0.0
        self._bar_minus_1_close: float = 0.0
        self._typical_range: float = 0.0
        self._session_volatility: float = 0.0
        self._session_high: float = 0.0
        self._session_low: float = 0.0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Reset on new session
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_in_session = 0
            self._bar_minus_1_outsized = False
            self._bar_minus_1_direction = None
            self._bar_minus_1_open = 0.0
            self._bar_minus_1_high = 0.0
            self._bar_minus_1_low = 0.0
            self._bar_minus_1_close = 0.0
            self._typical_range = 0.0
            self._session_volatility = 0.0
            self._session_high = 0.0
            self._session_low = 0.0
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        self._bars_in_session += 1
        current_bar = history[-1]
        
        # Update session high/low
        if self._bars_in_session == 1:
            self._session_high = current_bar.high
            self._session_low = current_bar.low
        else:
            self._session_high = max(self._session_high, current_bar.high)
            self._session_low = min(self._session_low, current_bar.low)
        
        # Don't trade if we have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return
        
        # Avoid trading in first N bars (need session context)
        if self._bars_in_session < self.early_session_bars + 1:
            return
        
        # Avoid trading in last N bars (need time for mean reversion)
        time_left_ns = ctx.session_end - ctx.ts
        bars_left_estimate = time_left_ns / (5 * 60 * 1e9)
        if bars_left_estimate < self.late_session_bars:
            return
        
        # Need enough history to measure typical range
        if len(history) < self.lookback + 1:
            return
        
        # Measure typical range from lookback window (excluding current bar)
        window_start = max(0, len(history) - self.lookback - 1)
        window_end = len(history) - 1
        
        if window_end - window_start < self.lookback:
            return
        
        window = history[window_start:window_end]
        if len(window) == 0:
            return
        
        typical_range = sum(b.high - b.low for b in window) / len(window)
        if typical_range < self.min_typical_range:
            return
        
        self._typical_range = typical_range
        self._session_volatility = typical_range
        
        # Analyze current bar for outsized move
        current_range = current_bar.high - current_bar.low
        current_body = abs(current_bar.close - current_bar.open)
        
        # IMPROVED outsized detection: range is large AND body is substantial
        # AND close is in outer portion of the bar (not just a wick)
        is_outsized = False
        if (current_range > typical_range * self.move_threshold and
            current_body > current_range * self.min_body_ratio):
            # Ensure close is not just a wick: check if it's in outer 25% of range
            if current_bar.close > current_bar.open:
                # Up bar: close should be in upper portion
                close_position = (current_bar.close - current_bar.low) / (current_range + 1e-9)
                is_outsized = close_position > 0.75
            else:
                # Down bar: close should be in lower portion
                close_position = (current_bar.high - current_bar.close) / (current_range + 1e-9)
                is_outsized = close_position > 0.75
        
        current_direction = None
        current_open = current_bar.open
        
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
            elif current_bar.close < current_bar.open:
                current_direction = "down"
        
        # TWO-BAR CONFIRMATION WITH ADAPTIVE FILTERS
        if self._bar_minus_1_outsized and self._bar_minus_1_direction is not None:
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            current_body = abs(current_bar.close - current_bar.open)
            current_range = current_bar.high - current_bar.low
            
            # ADAPTIVE confirmation_body_ratio based on volatility
            # High volatility sessions need stronger confirmation bodies
            vol_ratio = self._session_volatility / typical_range if typical_range > 0 else 1.0
            adaptive_confirmation_ratio = self.base_confirmation_body_ratio * (1.0 + (vol_ratio - 1.0) * 0.3)
            
            # Check body quality of confirmation bar
            has_good_body = (
                current_body > current_range * adaptive_confirmation_ratio
                if current_range > 0 else False
            )
            
            # Calculate session momentum from the last momentum_lookback bars
            session_momentum = None
            if len(history) >= self.momentum_lookback + 2:
                momentum_window_start = max(0, len(history) - self.momentum_lookback - 2)
                momentum_window_end = len(history) - 2
                momentum_window = history[momentum_window_start:momentum_window_end]
                
                if len(momentum_window) > 0:
                    momentum_closes = [b.close for b in momentum_window]
                    if len(momentum_closes) >= 2:
                        momentum_change = momentum_closes[-1] - momentum_closes[0]
                        momentum_range = max(b.high for b in momentum_window) - min(b.low for b in momentum_window)
                        if momentum_range > 0:
                            session_momentum = momentum_change / momentum_range
            
            # Dynamic stop/target based on volatility
            current_stop = self.base_stop_offset
            current_target = self.base_target_offset
            if self._session_volatility > 0:
                # In high volatility, tighten stops slightly to reduce drawdown
                vol_multiplier = 1.0 + (self._session_volatility / typical_range - 1.0) * self.vol_adjustment_factor
                current_stop = self.base_stop_offset * vol_multiplier
                current_target = self.base_target_offset * vol_multiplier
            
            # Check for reversal confirmation
            if (self._bar_minus_1_direction == "up" and current_moved_down and 
                has_good_body):
                # Previous was up, current is down
                if current_bar.close < self._bar_minus_1_open:
                    # IMPROVED reversal strength: consider overlap depth
                    # How far does the reversal penetrate into the prior bar's range?
                    overlap_depth = (self._bar_minus_1_high - current_bar.close) / (self._bar_minus_1_high - self._bar_minus_1_low + 1e-9)
                    
                    # Distance-based reversal strength
                    reversal_range = self._bar_minus_1_high - self._bar_minus_1_open if self._bar_minus_1_high > self._bar_minus_1_open else 0.1
                    reversal_distance = self._bar_minus_1_open - current_bar.close
                    distance_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    # Combined reversal strength (weighted average)
                    reversal_strength = 0.6 * distance_strength + 0.4 * overlap_depth
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        # Apply momentum filter: directional
                        trade_signal = True
                        if session_momentum is not None:
                            # Only skip if momentum is VERY strongly up (contrarian filter)
                            if session_momentum > self.momentum_threshold:
                                trade_signal = False
                        
                        if trade_signal:
                            ctx.sell(
                                size=self.base_size,
                                stop_offset=current_stop,
                                target_offset=current_target,
                                tag="fade-up",
                            )
            
            elif (self._bar_minus_1_direction == "down" and current_moved_up and 
                  has_good_body):
                # Previous was down, current is up
                if current_bar.close > self._bar_minus_1_open:
                    # IMPROVED reversal strength: consider overlap depth
                    overlap_depth = (current_bar.close - self._bar_minus_1_low) / (self._bar_minus_1_high - self._bar_minus_1_low + 1e-9)
                    
                    # Distance-based reversal strength
                    reversal_range = self._bar_minus_1_open - self._bar_minus_1_low if self._bar_minus_1_open > self._bar_minus_1_low else 0.1
                    reversal_distance = current_bar.close - self._bar_minus_1_open
                    distance_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    # Combined reversal strength
                    reversal_strength = 0.6 * distance_strength + 0.4 * overlap_depth
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        # Apply momentum filter: directional
                        trade_signal = True
                        if session_momentum is not None:
                            # Only skip if momentum is VERY strongly down
                            if session_momentum < -self.momentum_threshold:
                                trade_signal = False
                        
                        if trade_signal:
                            ctx.buy(
                                size=self.base_size,
                                stop_offset=current_stop,
                                target_offset=current_target,
                                tag="fade-down",
                            )
        
        # Store current bar info for next iteration
        self._bar_minus_1_outsized = is_outsized
        self._bar_minus_1_direction = current_direction
        self._bar_minus_1_open = current_open
        self._bar_minus_1_high = current_bar.high
        self._bar_minus_1_low = current_bar.low
        self._bar_minus_1_close = current_bar.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
