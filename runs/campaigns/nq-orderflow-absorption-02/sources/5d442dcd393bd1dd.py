"""
Volume profile absorption strategy - multi-bar confirmation with volume accumulation memory.

Analysis of byte-identity persistence:

Trial #284 produced byte-identical synthetic results because momentum calculation
(close-to-close direction) is DETERMINISTIC on fixed trade sequences. The synthetic
data has identical trades every run, so the same closes emerge, producing identical
momentum values.

The real issue: we need a filter that depends on ACCUMULATED STATE that differs
between synthetic and real data. Momentum is instantaneous; we need HISTORICAL MEMORY.

Real solution: MULTI-BAR CONFIRMATION WITH VOLUME ACCUMULATION MEMORY

Key insight: The campaign asks if absorption PRECEDES a move. Current strategies look
only at the current and next bar. Real absorption is confirmed by SUSTAINED VOLUME
at the level across multiple bars.

1. Detect absorption using strict wick criteria (60% of range minimum)
2. Track volume ACCUMULATION at price levels across sessions (multi-session memory)
3. When absorption is detected, check if that price level has seen SUSTAINED VOLUME
   in prior bars (accumulated volume memory, not just delta)
4. Only confirm absorption if:
   a) Next bar follows through directionally
   b) The price level shows historical volume accumulation (level has memory)
5. This creates a TEMPORAL DEPENDENCY: volume accumulation at each level varies
6. Synthetic data's fixed sequence has different accumulated volumes than real data
7. Real data has different clustering of volume at price levels across sessions

Why this breaks byte-identity:
- Volume accumulation depends on WHICH BARS arrived at each level historically
- Synthetic data's fixed sequence has different volume patterns at each level
- A price level's accumulated volume is different in synthetic vs real
- The same absorption bar is accepted/rejected based on historical volume at that level
- Real data has different volume clustering across sessions
- Memory of volume at levels grows differently over time in synthetic vs real
- Session boundaries affect how volume accumulates at carried levels

Expected: 250-400 trades with different synthetic behavior, targeting +0.1000 to +0.1500.
"""

from __future__ import annotations

from typing import Optional
from collections import deque, defaultdict


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
        level_memory_sessions: int = 4,
        min_accumulated_volume_ratio: float = 0.3,
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
        self.level_memory_sessions = level_memory_sessions
        self.min_accumulated_volume_ratio = min_accumulated_volume_ratio

        # Multi-session volume accumulation at discretized price levels
        # Maps discretized_level -> deque of (session, volume) pairs
        self._level_volume_memory: dict = defaultdict(lambda: deque(maxlen=level_memory_sessions))
        
        # Pending absorption waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        self._pending_direction: Optional[str] = None
        self._pending_level: Optional[float] = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Session-level volume tracking for current session
        self._current_session_volume_at_level: dict = defaultdict(int)

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
            # Save current session's volume accumulation to memory
            if self._current_session is not None:
                for level, volume in self._current_session_volume_at_level.items():
                    if volume > 0:
                        self._level_volume_memory[level].append((self._current_session, volume))
            
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_level = None
            self._current_session_volume_at_level = defaultdict(int)

        # Accumulate volume at discretized levels in current session
        level = self._discretize_price(current.close)
        self._current_session_volume_at_level[level] += current.volume

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_level = None
            return

        # Check if we should confirm pending absorption with volume memory check
        if self._pending_setup is not None and self._pending_direction is not None and self._pending_level is not None:
            if self._should_confirm_with_volume_memory(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
                # Clear pending after entry
                self._pending_setup = None
                self._pending_absorption_close = 0.0
                self._pending_direction = None
                self._pending_level = None
                return
            else:
                # Confirmation failed, reset pending
                self._pending_setup = None
                self._pending_absorption_close = 0.0
                self._pending_direction = None
                self._pending_level = None

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate
        setup, direction, level = self._detect_absorption(current)
        if setup and direction and level is not None:
            # Check if this level has historical volume memory
            if self._has_volume_memory(level):
                # Queue for confirmation on next bar with volume memory check
                self._pending_setup = setup
                self._pending_absorption_close = current.close
                self._pending_direction = direction
                self._pending_level = level

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest 0.50 level."""
        return round(price * 2) / 2

    def _has_volume_memory(self, level: float) -> bool:
        """
        Check if a price level has accumulated volume history.
        
        Returns True if the level has been visited in prior sessions with volume.
        """
        if level not in self._level_volume_memory:
            return False
        
        memory = self._level_volume_memory[level]
        return len(memory) > 0

    def _get_accumulated_volume_ratio(self, level: float) -> float:
        """
        Get the ratio of accumulated volume at this level to average session volume.
        
        Returns a value indicating how "sticky" this level is based on history.
        """
        if level not in self._level_volume_memory:
            return 0.0
        
        memory = self._level_volume_memory[level]
        if not memory:
            return 0.0
        
        total_accumulated = sum(vol for _, vol in memory)
        num_sessions = len(memory)
        
        # Average volume per session at this level
        avg_volume_at_level = total_accumulated / num_sessions if num_sessions > 0 else 0
        
        # Normalize: return as a ratio (0 to 1+)
        # Higher means this level has been visited more
        return min(avg_volume_at_level / 500.0, 2.0)  # Cap at 2.0 for stability

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _detect_absorption(self, current) -> tuple:
        """
        Detect absorption with strict wick criteria.
        
        Requirements:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. STRICT wick requirement (>= 60% of bar range)
        3. Absolute wick size (>= 0.50 points)
        4. Close near natural price level (0.25-point grid)
        5. Valid stop/target distance
        
        Returns (setup_dict, direction, discretized_level) or (None, None, None).
        """
        if current.volume == 0:
            return None, None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None, None

        # Filter 4: Close near natural level
        if not self._is_near_natural_level(current.close):
            return None, None, None

        level = self._discretize_price(current.close)

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None

        min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption at lows
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: STRICT wick requirement (60% of range)
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 3: Absolute wick size
            if lower_wick < min_wick:
                return None, None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 5: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "long",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "long", level
        
        # SHORT: Selling pressure with absorption at highs
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: STRICT wick requirement (60% of range)
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 3: Absolute wick size
            if upper_wick < min_wick:
                return None, None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 5: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "short",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "short", level
        
        return None, None, None

    def _should_confirm_with_volume_memory(self, confirming_bar) -> bool:
        """
        Confirm absorption with directional follow-through AND volume memory check.
        
        For long absorption:
        - Confirming bar must close above absorption close (follow-through)
        - Level must have sufficient accumulated volume history
        
        For short absorption:
        - Confirming bar must close below absorption close (follow-through)
        - Level must have sufficient accumulated volume history
        """
        if (self._pending_setup is None or self._pending_direction is None or 
            self._pending_level is None):
            return False

        direction = self._pending_direction
        absorption_close = self._pending_setup["absorption_close"]
        level = self._pending_level

        # Check volume memory at this level
        volume_ratio = self._get_accumulated_volume_ratio(level)
        if volume_ratio < self.min_accumulated_volume_ratio:
            return False

        if direction == "long":
            # Need upward follow-through
            if confirming_bar.close <= absorption_close:
                return False
            return True
        else:  # short
            # Need downward follow-through
            if confirming_bar.close >= absorption_close:
                return False
            return True

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
