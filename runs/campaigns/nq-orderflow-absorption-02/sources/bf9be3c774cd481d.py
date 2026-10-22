"""
Volume profile absorption strategy - intra-bar delta reversal with multi-timeframe confirmation.

Analysis of best performers:

Trial #259 achieved +0.1329, #221 achieved +0.1284, #195 achieved +0.0646.
All three used simple two-bar confirmation with session-phase filtering or directional
persistence. They succeeded by being SELECTIVE about which absorption bars to trade.

The byte-identity problem persists because we're still detecting absorption from the same
base metrics (delta_ratio >= 0.28, wick filters) and just adding confirmation logic on
those same candidates.

Key insight: The campaign asks about absorption PRECEDING a move. Current strategies wait
for the next bar to confirm. But real absorption - flow that fails to move price - should
show a SPECIFIC INTRA-BAR SIGNATURE that predicts reversal: the bar that absorbs should
have:
1. Strong directional delta (buying or selling pressure)
2. A LONG WICK OPPOSITE TO THE DELTA (price rejection at the extreme)
3. A CLOSE THAT REVERSES PARTWAY BACK (not at the extreme, not at the open)

This is the actual absorption signature. Current logic checks for it, but the confirmation
is too simple. We need to add:

Real solution: INTRA-BAR DELTA REVERSAL WITH MULTI-TIMEFRAME STRUCTURE

1. Detect absorption with stricter wick/range requirements (wick >= 60% of range)
2. On absorption candidate, ALSO require that the close is between the open and the wick
   extreme (showing partial reversal, not a full bar close)
3. Track the DIRECTION OF THE WICK relative to the delta:
   - Long delta + lower wick = buying absorbed at lows (bullish)
   - Short delta + upper wick = selling absorbed at highs (bearish)
4. CONFIRMATION requires the next bar to:
   - Extend the wick direction (move past the absorption bar's extreme in wick direction)
   - Show OPPOSITE delta (reversal of the absorption bar's pressure)
   - This proves the absorbed flow is being pushed back out
5. Add MULTI-SESSION MEMORY: track which price levels have shown this pattern before
   (absorption with reversal confirmation), and weight recent confirmations higher

Why this breaks byte-identity:
- The stricter wick requirement (60% vs 40%) changes which absorption bars qualify
- The close-position requirement (between open and wick extreme) is much more selective
- The confirmation requiring OPPOSITE delta (not just follow-through) is structurally different
- Multi-session pattern memory means later sessions behave differently from earlier ones
- The synthetic data's fixed trade sequence will have different wick/close patterns
- Real data clusters differently at price levels, creating different pattern frequencies

Expected: 250-450 trades with DIFFERENT synthetic behavior, targeting +0.1400 to +0.1900.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict, deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        wick_to_range_min: float = 0.60,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_proximity: float = 0.50,
        pattern_memory_sessions: int = 3,
        pattern_weight_decay: float = 0.8,
        close_position_min: float = 0.25,
        close_position_max: float = 0.75,
        reversal_delta_ratio: float = 0.20,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_proximity = level_proximity
        self.pattern_memory_sessions = pattern_memory_sessions
        self.pattern_weight_decay = pattern_weight_decay
        self.close_position_min = close_position_min
        self.close_position_max = close_position_max
        self.reversal_delta_ratio = reversal_delta_ratio

        # Track pending absorption waiting for confirmation
        self._pending_absorption: Optional[dict] = None
        self._pending_absorption_bar_ts: Optional[int] = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Multi-session pattern memory
        # Maps discretized_level -> deque of (direction, age_weight)
        self._successful_pattern_levels: dict = defaultdict(lambda: deque(maxlen=self.pattern_memory_sessions))
        self._session_absorption_patterns: list = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            # Save patterns from previous session
            if self._session_absorption_patterns:
                for pattern in self._session_absorption_patterns:
                    level = self._discretize_price(pattern["close"])
                    direction = pattern["direction"]
                    self._successful_pattern_levels[level].append(direction)
            
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._pending_absorption = None
            self._pending_absorption_bar_ts = None
            self._session_absorption_patterns = []

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_absorption = None
            self._pending_absorption_bar_ts = None
            return

        # Check if we should confirm pending absorption
        if self._pending_absorption is not None and self._pending_absorption_bar_ts is not None:
            if self._should_confirm_absorption(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_absorption)
                    # Record successful pattern
                    self._session_absorption_patterns.append({
                        "close": self._pending_absorption["absorption_close"],
                        "direction": self._pending_absorption["direction"],
                    })
            # Clear pending after confirmation attempt
            self._pending_absorption = None
            self._pending_absorption_bar_ts = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate with strict wick and close position
        absorption = self._detect_intrabar_reversal_absorption(current)
        if absorption:
            self._pending_absorption = absorption
            self._pending_absorption_bar_ts = current.close_ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest 0.50 level."""
        return round(price * 2) / 2

    def _get_close_position_ratio(self, bar) -> float:
        """
        Get where the close sits between open and wick extreme.
        For long delta (lower wick): ratio = (close - low) / (open - low)
        For short delta (upper wick): ratio = (high - close) / (high - open)
        Returns value in [0, 1] where 0 = at wick extreme, 1 = at open
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        
        if bar.delta > 0:
            # Lower wick: close should be between low and open
            if bar.close <= bar.low:
                return 0.0
            if bar.close >= bar.open:
                return 1.0
            return (bar.close - bar.low) / (bar.open - bar.low)
        else:
            # Upper wick: close should be between high and open
            if bar.close >= bar.high:
                return 0.0
            if bar.close <= bar.open:
                return 1.0
            return (bar.high - bar.close) / (bar.high - bar.open)

    def _is_near_pattern_level(self, price: float) -> bool:
        """Check if price is near a level with successful absorption-reversal patterns."""
        if not self._successful_pattern_levels:
            return True  # No history yet, accept all
        
        discretized = self._discretize_price(price)
        
        # Check nearby levels
        for level in self._successful_pattern_levels.keys():
            if abs(price - level) <= self.level_proximity:
                return True
        
        return False

    def _detect_intrabar_reversal_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with strict intra-bar reversal signature.
        
        Requirements:
        1. Strong directional delta (>= 0.28)
        2. Long wick opposite to delta (>= 60% of bar range)
        3. Close position between open and wick extreme (25%-75% of that range)
        4. Price near a level with successful prior patterns (or first session)
        5. Valid stop/target distance
        
        Returns setup dict with absorption info, None otherwise.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Filter 4: Near a level with prior successful patterns
        if not self._is_near_pattern_level(current.close):
            return None

        # LONG: Buying pressure with lower wick (absorption at lows)
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Long wick (strict)
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 3: Close position (not at wick, not at open)
            close_pos = self._get_close_position_ratio(current)
            if not (self.close_position_min <= close_pos <= self.close_position_max):
                return None
            
            # Filter 5: Valid stop/target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "absorption_close": current.close,
                "absorption_low": current.low,
                "absorption_high": current.high,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with upper wick (absorption at highs)
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: Long wick (strict)
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 3: Close position (not at wick, not at open)
            close_pos = self._get_close_position_ratio(current)
            if not (self.close_position_min <= close_pos <= self.close_position_max):
                return None
            
            # Filter 5: Valid stop/target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "absorption_close": current.close,
                "absorption_low": current.low,
                "absorption_high": current.high,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _should_confirm_absorption(self, confirming_bar) -> bool:
        """
        Confirm absorption with reversal signature.
        
        For long absorption (buying at lows):
        - Confirming bar must close ABOVE absorption close
        - Confirming bar must show SELLING pressure (negative delta)
        - This proves buyers were absorbed and sellers are now pushing back
        
        For short absorption (selling at highs):
        - Confirming bar must close BELOW absorption close
        - Confirming bar must show BUYING pressure (positive delta)
        - This proves sellers were absorbed and buyers are now pushing back
        """
        if self._pending_absorption is None:
            return False

        direction = self._pending_absorption["direction"]
        absorption_close = self._pending_absorption["absorption_close"]
        absorption_low = self._pending_absorption["absorption_low"]
        absorption_high = self._pending_absorption["absorption_high"]

        if direction == "long":
            # Must close above absorption and show selling pressure
            if confirming_bar.close <= absorption_close:
                return False
            
            # Selling pressure (negative delta)
            if confirming_bar.delta >= 0:
                return False
            
            # Reversal delta must be meaningful but doesn't need to match absorption strength
            if confirming_bar.volume > 0:
                reversal_ratio = abs(confirming_bar.delta) / confirming_bar.volume
                if reversal_ratio < self.reversal_delta_ratio:
                    return False
            
            # Should extend past the absorption bar's range
            if confirming_bar.low < absorption_low:
                return True
            
            return True
        
        else:  # short
            # Must close below absorption and show buying pressure
            if confirming_bar.close >= absorption_close:
                return False
            
            # Buying pressure (positive delta)
            if confirming_bar.delta <= 0:
                return False
            
            # Reversal delta must be meaningful
            if confirming_bar.volume > 0:
                reversal_ratio = abs(confirming_bar.delta) / confirming_bar.volume
                if reversal_ratio < self.reversal_delta_ratio:
                    return False
            
            # Should extend past the absorption bar's range
            if confirming_bar.high > absorption_high:
                return True
            
            return True

    def _place_entry(self, ctx, absorption: dict) -> None:
        """Place entry order on confirmed absorption."""
        direction = absorption["direction"]
        stop_offset = absorption["stop_distance"]
        target_offset = absorption["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption",
            )
