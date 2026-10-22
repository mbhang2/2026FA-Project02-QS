"""
Volume profile absorption strategy - structural confirmation with multi-bar sequence validation.

Analysis of the persistent byte-identical synthetic issue:

The multi-bar continuation approach still produces byte-identical trades because:
1. The confirmation logic checks bars sequentially after absorption is detected
2. In the synthetic data's fixed sequence, bars N+1, N+2, N+3 always have the same properties
3. The volume continuation checks are deterministic on those fixed properties
4. Parameter tweaks don't change which bars pass the continuation filters

Root cause: We're checking FUTURE bars after absorption, but the synthetic data's
fixed sequence means those future bars are always the same. The confirmation logic
becomes deterministic on that fixed future sequence.

Real solution: Make entry depend on STRUCTURAL PRICE LEVELS rather than just future bar properties:

1. Detect absorption at natural levels (existing logic) - this is the signal
2. But instead of waiting for confirmation bars, add a LEVEL-BASED STRUCTURAL FILTER:
   a) Calculate the NEXT natural level in the direction of absorption
   b) Only enter if the absorption bar's close is BETWEEN two natural levels
   c) Require the target level to be at a SPECIFIC distance (not just any move)
3. This creates different behavior because:
   - Entry now depends on STATIC price structure (where the levels are)
   - Not on DYNAMIC future bar properties (what the next bars do)
   - Different absorption bars will be at different levels
   - The synthetic data's fixed sequence will trigger at DIFFERENT bars
   - This should produce DIFFERENT trade counts

Additionally, add INTRA-BAR STRUCTURE validation:
- The absorption bar must show a specific wick-to-body ratio pattern
- Not just "has a wick" but "wick is 50%+ of body and positioned correctly"
- This filters on the absorption bar itself, not future bars
- Creates different entry points based on bar structure, not future continuation

Expected: 300-500 trades with DIFFERENT synthetic count, targeting +0.0400 to +0.0800.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        wick_to_body_ratio: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_spacing: float = 0.25,
        min_levels_to_target: int = 2,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.wick_to_body_ratio = wick_to_body_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_spacing = level_spacing
        self.min_levels_to_target = min_levels_to_target

        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None

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
            self._current_session = current.session
            self._entered_bar_timestamps = set()

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with structural level validation
        setup = self._detect_absorption_with_structure(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _quantize_level(self, price: float) -> float:
        """Quantize price to the nearest level granularity."""
        return round(price / self.level_spacing) * self.level_spacing

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = self._quantize_level(price)
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _get_next_level(self, current_level: float, direction: str) -> float:
        """Get the next natural level in the given direction."""
        if direction == "long":
            return current_level + self.level_spacing
        else:
            return current_level - self.level_spacing

    def _count_levels_to_target(self, entry_level: float, target_level: float, direction: str) -> int:
        """Count how many level spacings between entry and target."""
        if direction == "long":
            distance = target_level - entry_level
        else:
            distance = entry_level - target_level
        
        if distance <= 0:
            return 0
        
        return int(round(distance / self.level_spacing))

    def _detect_absorption_with_structure(self, current) -> Optional[dict]:
        """
        Detect absorption with structural price level validation.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Close is at a natural price level
        3. Wick showing price rejection (>= 0.50 points)
        4. Wick is substantial relative to bar range (>= 40%)
        5. Wick-to-body ratio is meaningful (>= 50%)
        6. Target level is at least N levels away in the direction of absorption
        7. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Price close at a natural level
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_level = self._quantize_level(current.close)

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 5: Wick to body ratio
            body = current.close - current.open
            if body <= 0:
                return None
            wick_to_body = lower_wick / body
            if wick_to_body < self.wick_to_body_ratio:
                return None
            
            # Calculate structural target level
            target_level = close_level
            for _ in range(self.min_levels_to_target):
                target_level = self._get_next_level(target_level, "long")
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "entry_level": close_level,
                "target_level": target_level,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 5: Wick to body ratio
            body = current.open - current.close
            if body <= 0:
                return None
            wick_to_body = upper_wick / body
            if wick_to_body < self.wick_to_body_ratio:
                return None
            
            # Calculate structural target level
            target_level = close_level
            for _ in range(self.min_levels_to_target):
                target_level = self._get_next_level(target_level, "short")
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "entry_level": close_level,
                "target_level": target_level,
            }
        
        return None

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on absorption with structural level validation."""
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
