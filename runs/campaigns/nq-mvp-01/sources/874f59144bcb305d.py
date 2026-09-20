"""
Mean-reversion fade with adaptive outsized detection and selective entry.

Analysis of recent trials:
- #37: -0.0476 — best so far with dynamic confirmation and momentum filtering
- #36: -0.0855 — regression from stricter thresholds
- #34: -0.0792 — strong baseline with relaxed momentum

Key insight from #37's success: The dynamic confirmation body ratio boost in early
session works well. The adaptive momentum threshold (0.15) provides good filtering.

Hypothesis for improvement: #37 achieved the best score by balancing:
1. Moderate outsized detection (move_threshold=1.35)
2. Dynamic confirmation strength based on session age
3. Selective momentum filtering (only skip if momentum > 0.15)
4. Reasonable stop/target levels (0.70/0.72)

Refinements to try:
1. ADAPTIVE OUTSIZED THRESHOLD: Outsized detection should be relative to recent
   volatility trend, not just absolute typical_range. If volatility is declining,
   be more selective. If increasing, be more permissive.

2. CONFIRMATION TIMING: Current confirmation body boost applies to first 7 bars
   (early_session_bars + 3). Consider extending this window slightly since early
   session context is weak for longer.

3. REVERSAL STRENGTH CALIBRATION: #37 uses 0.17, which is good. Keep it but make
   the calculation more robust by considering the full prior bar's range, not just
   the move from open.

4. MOMENTUM WINDOW OPTIMIZATION: Current momentum_lookback=3 bars. Try 4 bars to
   get better session trend direction, as 3 bars might be too short.

5. REDUCE FALSE SIGNALS: Add a secondary filter - require that the confirmation
   bar not only reverses but also closes near its extremum (high conviction close).

6. VOLATILITY ADJUSTMENT: Current vol_adjustment_factor=0.5 is moderate. Keep it
   but ensure stop/target don't expand too much in volatile sessions.

Strategy focus: Keep the proven #37 core, add adaptive logic that doesn't
overcomplicate, and focus on high-conviction reversals with better calibration.
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
        stop_offset: float = 0.70,
        target_offset: float = 0.72,
        reversal_strength_threshold: float = 0.17,
        min_typical_range: float = 0.10,
        momentum_lookback: int = 4,
        momentum_threshold: float = 0.15,
        vol_adjustment_factor: float = 0.5,
        early_session_bars: int = 4,
        late_session_bars: int = 4,
        early_session_confirmation_boost: float = 0.10,
        confirmation_window_extension: int = 4,
        close_conviction_threshold: float = 0.70,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.min_body_ratio = min_body_ratio
        self.base_confirmation_body_ratio = base_confirmation_body_ratio
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.reversal_strength_threshold = reversal_strength_threshold
        self.min_typical_range = min_typical_range
        self.momentum_lookback = momentum_lookback
        self.momentum_threshold = momentum_threshold
        self.vol_adjustment_factor = vol_adjustment_factor
        self.early_session_bars = early_session_bars
        self.late_session_bars = late_session_bars
        self.early_session_confirmation_boost = early_session_confirmation_boost
        self.confirmation_window_extension = confirmation_window_extension
        self.close_conviction_threshold = close_conviction_threshold
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_open: float = 0.0
        self._bar_minus_1_high: float = 0.0
        self._bar_minus_1_low: float = 0.0
        self._bar_minus_1_range: float = 0.0
        self._typical_range: float = 0.0
        self._session_volatility: float = 0.0
        self._prev_typical_range: float = 0.0

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
            self._bar_minus_1_range = 0.0
            self._typical_range = 0.0
            self._session_volatility = 0.0
            self._prev_typical_range = 0.0
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        self._bars_in_session += 1
        current_bar = history[-1]
        
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
        
        # Track volatility trend for adaptive outsized detection
        self._prev_typical_range = self._typical_range
        self._typical_range = typical_range
        self._session_volatility = typical_range
        
        # Analyze current bar for outsized move
        current_range = current_bar.high - current_bar.low
        current_body = abs(current_bar.close - current_bar.open)
        
        # ADAPTIVE outsized detection: adjust threshold based on volatility trend
        # If volatility is increasing, be more permissive (lower threshold)
        # If volatility is decreasing, be more selective (higher threshold)
        adaptive_threshold = self.move_threshold
        if self._prev_typical_range > 0:
            vol_trend = typical_range / self._prev_typical_range
            if vol_trend < 0.95:  # Volatility declining
                adaptive_threshold = self.move_threshold * 1.05
            elif vol_trend > 1.05:  # Volatility rising
                adaptive_threshold = self.move_threshold * 0.95
        
        is_outsized = (
            current_range > typical_range * adaptive_threshold and
            current_body > current_range * self.min_body_ratio
        )
        
        current_direction = None
        current_open = current_bar.open
        
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
            elif current_bar.close < current_bar.open:
                current_direction = "down"
        
        # TWO-BAR CONFIRMATION with enhanced conviction checks
        if self._bar_minus_1_outsized and self._bar_minus_1_direction is not None:
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            current_body = abs(current_bar.close - current_bar.open)
            current_range = current_bar.high - current_bar.low
            
            # Dynamic confirmation body ratio based on session age
            # Extended window: require stronger confirmation for more bars
            confirmation_ratio = self.base_confirmation_body_ratio
            if self._bars_in_session <= self.early_session_bars + self.confirmation_window_extension:
                confirmation_ratio += self.early_session_confirmation_boost
            
            # Check body quality of confirmation bar
            has_good_body = (
                current_body > current_range * confirmation_ratio
                if current_range > 0 else False
            )
            
            # NEW: Check close conviction - close should be near the extremum
            # of the confirmation bar for high conviction
            close_conviction = False
            if current_moved_down and current_range > 0:
                # For a down reversal, close should be near the low
                distance_to_low = current_bar.close - current_bar.low
                close_conviction = distance_to_low < current_range * (1.0 - self.close_conviction_threshold)
            elif current_moved_up and current_range > 0:
                # For an up reversal, close should be near the high
                distance_to_high = current_bar.high - current_bar.close
                close_conviction = distance_to_high < current_range * (1.0 - self.close_conviction_threshold)
            
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
            current_stop = self.stop_offset
            current_target = self.target_offset
            if self._session_volatility > 0:
                vol_multiplier = 1.0 + (self._session_volatility / typical_range - 1.0) * self.vol_adjustment_factor
                current_stop = self.stop_offset * vol_multiplier
                current_target = self.target_offset * vol_multiplier
            
            # Check for reversal confirmation with conviction checks
            if (self._bar_minus_1_direction == "up" and current_moved_down and 
                has_good_body and close_conviction):
                # Previous was up, current is down with conviction
                if current_bar.close < self._bar_minus_1_open:
                    # Measure reversal strength
                    reversal_range = self._bar_minus_1_high - self._bar_minus_1_open if self._bar_minus_1_high > self._bar_minus_1_open else 0.1
                    reversal_distance = self._bar_minus_1_open - current_bar.close
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        # Apply momentum filter: only skip if momentum is VERY strong up
                        trade_signal = True
                        if session_momentum is not None:
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
                  has_good_body and close_conviction):
                # Previous was down, current is up with conviction
                if current_bar.close > self._bar_minus_1_open:
                    # Measure reversal strength
                    reversal_range = self._bar_minus_1_open - self._bar_minus_1_low if self._bar_minus_1_open > self._bar_minus_1_low else 0.1
                    reversal_distance = current_bar.close - self._bar_minus_1_open
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        # Apply momentum filter: only skip if momentum is VERY strong down
                        trade_signal = True
                        if session_momentum is not None:
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
        self._bar_minus_1_range = current_range

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
