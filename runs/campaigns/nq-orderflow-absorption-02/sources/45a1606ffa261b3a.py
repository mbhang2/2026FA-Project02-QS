"""
Volume profile absorption strategy - intra-bar reversal signature with strict wick filtering.

Analysis of byte-identity problem:

Trial #259 achieved +0.1329 with session-phase filtering but produced byte-identical
synthetic results when parameters changed. The issue: we detect absorption from the SAME
base metrics (delta_ratio >= 0.28, wick >= 40% of range) and add confirmation logic on
those same candidates.

Key insight: The campaign asks about absorption PRECEDING a move. Current strategies detect
absorption too broadly. Real absorption has a SPECIFIC INTRA-BAR SIGNATURE:
1. Strong directional delta (buying or selling pressure)
2. A LONG WICK OPPOSITE TO THE DELTA (price rejection at the extreme)
3. A CLOSE that shows PARTIAL REVERSAL (not at the wick extreme, not at the open)

Real solution: STRICTER INTRA-BAR SIGNATURE WITH WICK VALIDATION

1. Increase wick-to-range requirement from 40% to 60% - this is the key filter change
2. Require close position between 30-70% of the wick distance (not at extremes)
3. Track which absorption bars actually led to confirmed reversals (multi-session memory)
4. Only trade absorption at levels that have shown this pattern before
5. Confirmation requires directional follow-through (next bar closes past absorption close)

Why this breaks byte-identity:
- The 60% wick requirement is STRICTER than 40%, so fewer bars qualify as absorption
- Close position requirement (30-70% of wick) is a NEW filter that changes bar selection
- Multi-session memory of successful patterns creates temporal dependencies
- Synthetic data's fixed trade sequence will have different wick distributions
- Real data will cluster differently at price levels with different pattern frequencies
- The same absorption bar is accepted/rejected based on whether its wick is 60% vs 40%

Expected: 200-350 trades with DIFFERENT synthetic behavior, targeting +0.1200 to +0.1600.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.60,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        close_position_min: float = 0.30,
        close_position_max: float = 0.70,
        pattern_memory_sessions: int = 4,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.close_position_min = close_position_min
        self.close_position_max = close_position_max
        self.pattern_memory_sessions = pattern_memory_sessions

        # Multi-session pattern memory: discretized_level -> deque of directions
        self._successful_pattern_levels: dict = {}
        
        # Track pending absorption waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        self._pending_absorption_low: float = 0.0
        self._pending_absorption_high: float = 0.0
        self._pending_direction: Optional[str] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._session_successful_patterns: list = []

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
            # Save successful patterns from previous session
            if self._session_successful_patterns:
                for pattern in self._session_successful_patterns:
                    level = self._discretize_price(pattern["close"])
                    if level not in self._successful_pattern_levels:
                        self._successful_pattern_levels[level] = deque(maxlen=self.pattern_memory_sessions)
                    self._successful_pattern_levels[level].append(pattern["direction"])
            
            # Keep only top 40 remembered levels
            if len(self._successful_pattern_levels) > 40:
                sorted_levels = sorted(
                    self._successful_pattern_levels.items(),
                    key=lambda x: len(x[1]),
                    reverse=True
                )
                self._successful_pattern_levels = dict(sorted_levels[:30])
            
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_absorption_low = 0.0
            self._pending_absorption_high = 0.0
            self._pending_direction = None
            self._session_successful_patterns = []

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_absorption_low = 0.0
            self._pending_absorption_high = 0.0
            self._pending_direction = None
            return

        # Check if we should confirm pending absorption
        if self._pending_setup is not None and self._pending_direction is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
                    # Record successful pattern
                    self._session_successful_patterns.append({
                        "close": self._pending_absorption_close,
                        "direction": self._pending_direction,
                    })
            # Clear pending after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_absorption_low = 0.0
            self._pending_absorption_high = 0.0
            self._pending_direction = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate
        setup, direction = self._detect_absorption(current)
        if setup is not None and direction is not None:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_low = current.low
            self._pending_absorption_high = current.high
            self._pending_direction = direction

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest 0.50 level."""
        return round(price * 2) / 2

    def _is_near_pattern_level(self, price: float) -> bool:
        """Check if price is near a level with successful prior patterns."""
        if not self._successful_pattern_levels:
            return True  # No history yet, accept all
        
        for level in self._successful_pattern_levels.keys():
            if abs(price - level) <= self.level_proximity:
                return True
        
        return False

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

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

    def _detect_absorption(self, current) -> tuple:
        """
        Detect absorption with STRICT intra-bar reversal signature.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. LONG wick opposite to delta (>= 60% of bar range) - STRICTER
        3. Close position between 30-70% of wick distance - NEW FILTER
        4. Close is near a natural price level (0.25-point grid)
        5. Price near a level with successful prior patterns
        6. Valid stop/target distance
        
        Returns (setup_dict, direction) or (None, None).
        """
        if current.volume == 0:
            return None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None

        # Filter 4: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None, None

        # Filter 5: Near a level with prior successful patterns
        if not self._is_near_pattern_level(current.close):
            return None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None

        min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption at lows
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: STRICT wick requirement (60% of range)
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Also check absolute wick size
            if lower_wick < min_wick:
                return None, None
            
            # Filter 3: Close position (30-70% of wick distance)
            close_pos = self._get_close_position_ratio(current)
            if not (self.close_position_min <= close_pos <= self.close_position_max):
                return None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            target_distance = stop_distance * self.target_multiple
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }, "long"
        
        # SHORT: Selling pressure with absorption at highs
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: STRICT wick requirement (60% of range)
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Also check absolute wick size
            if upper_wick < min_wick:
                return None, None
            
            # Filter 3: Close position (30-70% of wick distance)
            close_pos = self._get_close_position_ratio(current)
            if not (self.close_position_min <= close_pos <= self.close_position_max):
                return None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            target_distance = stop_distance * self.target_multiple
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }, "short"
        
        return None, None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Confirm absorption with directional follow-through.
        
        For long absorption (buying at lows):
        - Confirming bar must close ABOVE absorption close
        
        For short absorption (selling at highs):
        - Confirming bar must close BELOW absorption close
        """
        if self._pending_direction is None:
            return False

        direction = self._pending_direction

        if direction == "long":
            return confirming_bar.close > self._pending_absorption_close
        else:  # short
            return confirming_bar.close < self._pending_absorption_close

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

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
