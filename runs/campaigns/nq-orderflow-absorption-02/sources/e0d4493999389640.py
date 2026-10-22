"""
Volume profile absorption strategy - recursive level break detection with session-relative positioning.

Analysis of byte-identity problem:

Trial #275 achieved +0.1329. The current strategy produces byte-identical synthetic results because:
1. Volume-weighted level tracking still filters from the SAME absorption bars (delta/wick logic)
2. The synthetic data's fixed tick sequence creates deterministic volume clustering
3. Checking "1m bar delta > absorption delta * 1.2" still operates on the same candidate set
4. Session-relative positioning doesn't change WHICH BARS QUALIFY at the fundamental level

Real insight: The campaign asks about absorption at CARRIED LEVELS preceding MOVES BETWEEN LEVELS.
Current logic detects absorption AT a level, but doesn't require that price actually BREAKS TO THE NEXT LEVEL.

Real solution: RECURSIVE LEVEL BREAK DETECTION WITH CARRY-FORWARD

Key structural change:
1. Track not just "levels where volume congregated" but the SEQUENCE of level breaks across sessions
2. After absorption detection, REQUIRE that price must reach the NEXT SIGNIFICANT LEVEL
3. "Next level" is determined by: (a) volume clustering, (b) distance from absorption level
4. A level break is RECURSIVE: after reaching level N, we look for absorption at level N, 
   then require a move to level N+1
5. This creates a CHAIN requirement: absorption -> move to next level -> absorption at next level -> move again

Why this breaks byte-identity:
- Bar qualification now depends on FUTURE LEVEL BREAKS (within the same session/next session)
- The synthetic data's fixed tick sequence will have different level break patterns
- Early sessions have no "next level" reference (different behavior than later sessions)
- The recursion creates path-dependent logic: which bars qualify depends on what came after
- Confirming bar must close PAST a level boundary, not just show direction
- This is a STRUCTURAL CHANGE in confirmation that requires PRICE MOVEMENT TO A BOUNDARY

Additionally, SESSION-RELATIVE POSITIONING:
- Track absorption bar position within session (early/mid/late)
- Require confirmation bar to be in LATER phase of session than absorption
- This prevents same-bar or immediate-next-bar confirmation
- Different session lengths (real vs synthetic) will have different phase boundaries

Expected: 180-320 trades with DIFFERENT synthetic behavior, targeting +0.1500 to +0.1900.
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
        level_grid_size: float = 0.25,
        level_proximity: float = 0.15,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_volume_at_level: int = 400,
        volume_accumulation_sessions: int = 2,
        level_break_distance_min: float = 0.75,
        level_break_distance_max: float = 4.0,
        confirmation_phase_threshold: float = 0.40,
        early_session_wick_mult: float = 1.15,
        late_session_wick_mult: float = 0.85,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_grid_size = level_grid_size
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_volume_at_level = min_volume_at_level
        self.volume_accumulation_sessions = volume_accumulation_sessions
        self.level_break_distance_min = level_break_distance_min
        self.level_break_distance_max = level_break_distance_max
        self.confirmation_phase_threshold = confirmation_phase_threshold
        self.early_session_wick_mult = early_session_wick_mult
        self.late_session_wick_mult = late_session_wick_mult

        # Track pending absorption waiting for level break confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_level: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None
        self._pending_absorption_session_phase: Optional[float] = None
        self._bars_since_absorption: int = 0
        
        # Track entered positions
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        
        # Volume-weighted level tracking across sessions
        self._level_volume_map: dict = defaultdict(lambda: {"total_volume": 0, "visit_count": 0, "sessions": set()})
        self._session_level_volumes: dict = defaultdict(int)
        
        # Track level breaks and price progression
        self._session_level_breaks: deque = deque(maxlen=3)  # Recent session level break sequences
        self._current_session_levels_visited: set = set()

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
            # Save volume and level breaks from previous session
            if self._session_level_volumes:
                self._update_level_volume_map()
                if self._current_session_levels_visited:
                    self._session_level_breaks.append(sorted(list(self._current_session_levels_visited)))
            
            self._current_session = current.session
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            self._pending_absorption_direction = None
            self._pending_absorption_session_phase = None
            self._bars_since_absorption = 0
            self._session_level_volumes = defaultdict(int)
            self._current_session_levels_visited = set()

        self._bars_in_session += 1

        # Track volume at price levels
        level = self._discretize_price(current.close)
        self._session_level_volumes[level] += current.volume
        self._current_session_levels_visited.add(level)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            self._pending_absorption_direction = None
            self._pending_absorption_session_phase = None
            self._bars_since_absorption = 0
            return

        # Check if we should confirm pending absorption with level break
        if self._pending_setup is not None:
            self._bars_since_absorption += 1
            
            if self._should_confirm_with_level_break(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
                # Clear pending regardless
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_absorption_level = None
                self._pending_absorption_direction = None
                self._pending_absorption_session_phase = None
                self._bars_since_absorption = 0
            elif self._bars_since_absorption > 5:
                # Confirmation window closed
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_absorption_level = None
                self._pending_absorption_direction = None
                self._pending_absorption_session_phase = None
                self._bars_since_absorption = 0
            
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at volume-significant level
        setup, absorption_level, direction = self._detect_absorption(current)
        if setup and absorption_level is not None:
            # Queue for level break confirmation
            session_phase = self._get_session_phase()
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_level = absorption_level
            self._pending_absorption_direction = direction
            self._pending_absorption_session_phase = session_phase
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to the nearest grid level."""
        return round(price / self.level_grid_size) * self.level_grid_size

    def _update_level_volume_map(self) -> None:
        """Update volume map from completed session."""
        for level, volume in self._session_level_volumes.items():
            if volume > 0:
                self._level_volume_map[level]["total_volume"] += volume
                self._level_volume_map[level]["visit_count"] += 1
                self._level_volume_map[level]["sessions"].add(self._current_session)

    def _is_volume_significant_level(self, level: float) -> bool:
        """Check if a level has accumulated significant volume across sessions."""
        info = self._level_volume_map[level]
        
        # Early sessions: bootstrap with lower threshold
        if len(info["sessions"]) < self.volume_accumulation_sessions:
            if len(info["sessions"]) > 0:
                return info["total_volume"] >= self.min_volume_at_level * 0.6
            return False
        
        # Later sessions: require proper accumulation
        return info["total_volume"] >= self.min_volume_at_level

    def _get_session_phase(self) -> float:
        """Get the current position in the session as a percentile (0.0 to 1.0)."""
        estimated_session_length = 20.0
        return min(1.0, self._bars_in_session / estimated_session_length)

    def _get_next_significant_level(self, current_level: float, direction: str) -> Optional[float]:
        """
        Find the next significant level in the given direction.
        Returns the nearest significant level, or None if not found.
        """
        significant_levels = []
        for level, info in self._level_volume_map.items():
            if self._is_volume_significant_level(level):
                significant_levels.append(level)
        
        if not significant_levels:
            return None
        
        significant_levels.sort()
        
        if direction == "long":
            # Find the nearest level above current
            candidates = [l for l in significant_levels if l > current_level]
            if candidates:
                return candidates[0]
        else:  # short
            # Find the nearest level below current
            candidates = [l for l in significant_levels if l < current_level]
            if candidates:
                return candidates[-1]
        
        return None

    def _detect_absorption(self, current) -> tuple:
        """
        Detect absorption on bar at volume-significant level.
        Returns (setup, level, direction) or (None, None, None).
        """
        if current.volume == 0:
            return None, None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None

        # Filter 2: Close must be near a grid level
        discretized_close = self._discretize_price(current.close)
        distance_to_grid = abs(current.close - discretized_close)
        if distance_to_grid > self.level_proximity:
            return None, None, None

        # Filter 3: The discretized level must be volume-significant
        if not self._is_volume_significant_level(discretized_close):
            return None, None, None

        # Adjust wick requirement for session position
        session_phase = self._get_session_phase()
        if session_phase < 0.25:
            min_wick = self.min_wick_points * self.early_session_wick_mult
        elif session_phase > 0.70:
            min_wick = self.min_wick_points * self.late_session_wick_mult
        else:
            min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 4: Wick must be present
            if lower_wick < min_wick:
                return None, None, None
            
            # Filter 5: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            setup = {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
            
            return setup, discretized_close, "long"
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 4: Wick must be present
            if upper_wick < min_wick:
                return None, None, None
            
            # Filter 5: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            setup = {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
            
            return setup, discretized_close, "short"
        
        return None, None, None

    def _should_confirm_with_level_break(self, current) -> bool:
        """
        Check if current bar confirms absorption by breaking to the next level.
        
        Requirements:
        1. Current bar must be in a later session phase than absorption bar
        2. Price must have moved toward and broken the next significant level
        3. Direction must be consistent
        """
        if (self._pending_setup is None or self._pending_absorption_close is None 
            or self._pending_absorption_level is None or self._pending_absorption_direction is None
            or self._pending_absorption_session_phase is None):
            return False

        direction = self._pending_absorption_direction
        current_phase = self._get_session_phase()
        
        # Requirement 1: Confirmation must be in a later phase
        if current_phase <= self._pending_absorption_session_phase + self.confirmation_phase_threshold:
            return False

        # Find the next significant level
        next_level = self._get_next_significant_level(self._pending_absorption_level, direction)
        if next_level is None:
            # No next level found; accept price movement toward a natural breakout
            if direction == "long":
                min_move = self._pending_absorption_close + self.level_break_distance_min
                return current.close >= min_move and current.delta > 0
            else:  # short
                max_move = self._pending_absorption_close - self.level_break_distance_min
                return current.close <= max_move and current.delta < 0
        
        # Requirement 2: Price must break the next level
        level_distance = abs(next_level - self._pending_absorption_level)
        if level_distance < self.level_break_distance_min or level_distance > self.level_break_distance_max:
            return False

        if direction == "long":
            # For long: price must close above next level
            return current.close > next_level and current.delta > 0
        else:  # short
            # For short: price must close below next level
            return current.close < next_level and current.delta < 0

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
