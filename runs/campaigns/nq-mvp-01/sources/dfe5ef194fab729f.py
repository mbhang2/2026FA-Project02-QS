"""
Mean-reversion fade with multi-bar confirmation and adaptive range normalization.

Analysis of recent trials:
- #33: -0.1342 — best so far, uses session momentum filtering
- #29: -0.1707 — strict body ratio on outsized moves
- #32: -0.2399 — relaxed outsized detection with strict reversal conviction
- #6: -0.3638 — all-time champion with simpler logic

Key insight from #33: Session momentum filtering helps, but the current approach
may be too restrictive. Looking at the pattern:
- Strict outsized detection (body_ratio 0.75) works well
- Two-bar confirmation is solid
- Session momentum filtering reduces false signals

Hypothesis: The momentum filter in #33 is too aggressive (momentum_threshold=0.05
might be filtering out too many valid trades). Also, we might benefit from:
1. More lenient momentum threshold to let more contrarian trades through
2. Better normalization of the reversal strength calculation
3. Slightly relaxed outsized move detection to catch more opportunities
4. Add a volatility-based dynamic stop/target sizing

New approach:
- Keep the two-bar confirmation pattern (proven)
- Relax momentum_threshold from 0.05 to 0.10 (less filtering)
- Adjust move_threshold from 1.40 to 1.35 (catch slightly smaller outsized moves)
- Improve reversal strength calculation with better range normalization
- Add volatility-based dynamic sizing (larger stops/targets in high vol sessions)
- Keep the core momentum filter but make it less aggressive
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
        confirmation_body_ratio: float = 0.60,
        base_size: int = 1,
        stop_offset: float = 0.75,
        target_offset: float = 0.75,
        reversal_strength_threshold: float = 0.15,
        min_typical_range: float = 0.10,
        momentum_lookback: int = 3,
        momentum_threshold: float = 0.10,
        vol_adjustment_factor: float = 0.5,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.move_threshold = move_threshold
        self.min_body_ratio = min_body_ratio
        self.confirmation_body_ratio = confirmation_body_ratio
        self.base_size = base_size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.reversal_strength_threshold = reversal_strength_threshold
        self.min_typical_range = min_typical_range
        self.momentum_lookback = momentum_lookback
        self.momentum_threshold = momentum_threshold
        self.vol_adjustment_factor = vol_adjustment_factor
        
        self._session: int | None = None
        self._bars_in_session: int = 0
        self._bar_minus_1_outsized: bool = False
        self._bar_minus_1_direction: str | None = None
        self._bar_minus_1_open: float = 0.0
        self._bar_minus_1_high: float = 0.0
        self._bar_minus_1_low: float = 0.0
        self._typical_range: float = 0.0
        self._session_volatility: float = 0.0

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
            self._typical_range = 0.0
            self._session_volatility = 0.0
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        self._bars_in_session += 1
        current_bar = history[-1]
        
        # Don't trade if we have a position or working orders
        if ctx.position or ctx.working_order_ids:
            return
        
        # Avoid trading in first 4 bars (need session context)
        if self._bars_in_session < 5:
            return
        
        # Avoid trading in last 4 bars (need time for mean reversion)
        time_left_ns = ctx.session_end - ctx.ts
        bars_left_estimate = time_left_ns / (5 * 60 * 1e9)
        if bars_left_estimate < 4.0:
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
        
        # Calculate session volatility for dynamic stop/target sizing
        self._session_volatility = typical_range
        
        # Analyze current bar for outsized move
        current_range = current_bar.high - current_bar.low
        current_body = abs(current_bar.close - current_bar.open)
        
        # RELAXED outsized detection: slightly lower threshold to catch more moves
        is_outsized = (
            current_range > typical_range * self.move_threshold and
            current_body > current_range * self.min_body_ratio
        )
        
        current_direction = None
        current_open = current_bar.open
        
        if is_outsized:
            if current_bar.close > current_bar.open:
                current_direction = "up"
            elif current_bar.close < current_bar.open:
                current_direction = "down"
        
        # TWO-BAR CONFIRMATION WITH RELAXED SESSION MOMENTUM FILTER
        # Previous bar was outsized, current bar must:
        # 1. Move opposite direction (reversal)
        # 2. Have meaningful body
        # 3. Close beyond prior bar's open (progression)
        # 4. Show ANY reversal conviction (threshold=0.15)
        # 5. Be CONTRARIAN to session momentum (relaxed filter)
        
        if self._bar_minus_1_outsized and self._bar_minus_1_direction is not None:
            current_moved_up = current_bar.close > current_bar.open
            current_moved_down = current_bar.close < current_bar.open
            current_body = abs(current_bar.close - current_bar.open)
            current_range = current_bar.high - current_bar.low
            
            # Check body quality of confirmation bar
            has_good_body = (
                current_body > current_range * self.confirmation_body_ratio
                if current_range > 0 else False
            )
            
            # Calculate session momentum from the last momentum_lookback bars
            # (excluding the current and previous bar which are part of our signal)
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
            
            # Check for reversal confirmation with progression and momentum filter
            if (self._bar_minus_1_direction == "up" and current_moved_down and 
                has_good_body):
                # Previous was up, current is down: check if close is below prior open
                if current_bar.close < self._bar_minus_1_open:
                    # Measure reversal strength with better normalization
                    reversal_range = self._bar_minus_1_high - self._bar_minus_1_open if self._bar_minus_1_high > self._bar_minus_1_open else 0.1
                    reversal_distance = self._bar_minus_1_open - current_bar.close
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        # Apply momentum filter: RELAXED version
                        trade_signal = True
                        if session_momentum is not None:
                            # Only skip if momentum is VERY strongly up
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
                # Previous was down, current is up: check if close is above prior open
                if current_bar.close > self._bar_minus_1_open:
                    # Measure reversal strength with better normalization
                    reversal_range = self._bar_minus_1_open - self._bar_minus_1_low if self._bar_minus_1_open > self._bar_minus_1_low else 0.1
                    reversal_distance = current_bar.close - self._bar_minus_1_open
                    reversal_strength = min(1.0, reversal_distance / reversal_range if reversal_range > 0 else 0.5)
                    
                    if reversal_strength >= self.reversal_strength_threshold:
                        # Apply momentum filter: RELAXED version
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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
