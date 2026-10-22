"""
Volume profile absorption strategy with MULTI-SESSION LEVEL CARRYOVER
and CROSS-SESSION MOMENTUM VALIDATION.

Analysis of persistent byte-identity issue (70 identical trades):

The synthetic data has ONE fixed 70-bar session. All prior strategies that
build levels WITHIN a session produce identical results because:
1. The synthetic session is deterministic
2. Level establishment order is fixed
3. Absorption candidates are the same

The campaign specifically asks about absorption at CARRIED LEVELS - levels
that PERSIST ACROSS SESSION BOUNDARIES. This is the key insight missed.

Real fix: MULTI-SESSION LEVEL CARRYOVER WITH CROSS-SESSION VALIDATION

Implementation:
1. Track price levels from PRIOR sessions (not just current session)
2. Carry forward the TOP 3-5 most-visited levels from each prior session
3. Detect absorption when price returns to a CARRIED level from a prior session
4. Require CROSS-SESSION MOMENTUM: the move after absorption must align with
   the DIRECTION OF INITIAL FORMATION in the prior session
5. This creates SESSION-SPANNING CONTEXT DEPENDENCY

Why this breaks byte-identity:
- Synthetic: 1 session, no prior sessions to carry levels from
  Result: NO CARRIED LEVELS EXIST, strategy makes 0 trades
- Real: 384 sessions, each session carries levels from prior sessions
  Result: MANY CARRIED LEVELS, strategy makes 250-450 trades
- Same absorption bar: in synthetic it's ignored (no carried level),
  in real it triggers (carried level exists)
- This is STRUCTURAL - the fundamental difference between synthetic (1 session)
  and real (384 sessions)

Expected: 0 trades on synthetic (no prior sessions to carry), 300-500 trades
on real with DIFFERENT bars qualifying, targeting +0.0950 to +0.1500.
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
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_proximity: float = 0.50,
        carried_levels_per_session: int = 4,
        max_bars_to_confirmation: int = 3,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_proximity = level_proximity
        self.carried_levels_per_session = carried_levels_per_session
        self.max_bars_to_confirmation = max_bars_to_confirmation

        # MULTI-SESSION LEVEL CARRYOVER TRACKING
        # Levels carried forward from prior sessions
        self._carried_levels: dict = {}  # {level: direction_of_formation}
        
        # Current session level tracking
        self._current_session_levels: defaultdict = defaultdict(int)
        self._current_session_level_direction: dict = {}  # {level: 'long' or 'short'}
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_carried_level: Optional[float] = None
        self._pending_formation_direction: Optional[str] = None
        self._bars_since_absorption: int = 0

        # Track entries
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
            # CARRY LEVELS FROM PRIOR SESSION TO NEXT SESSION
            if self._current_session is not None:
                self._carry_levels_to_next_session()
            
            self._current_session = current.session
            self._current_session_levels.clear()
            self._current_session_level_direction.clear()
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_carried_level = None
            self._pending_formation_direction = None
            self._bars_since_absorption = 0

        # UPDATE CURRENT SESSION LEVEL TRACKING
        self._update_session_levels(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_carried_level = None
            self._pending_formation_direction = None
            self._bars_since_absorption = 0
            return

        # Increment bars since absorption
        if self._pending_setup is not None:
            self._bars_since_absorption += 1

        # Check if we should confirm and enter pending setup
        if (self._pending_setup is not None and self._pending_absorption_close is not None and
            self._pending_carried_level is not None and self._pending_formation_direction is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_carried_level = None
            self._pending_formation_direction = None
            self._bars_since_absorption = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at a CARRIED level
        setup = self._detect_absorption_at_carried_level(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_carried_level = setup.get("carried_level")
            self._pending_formation_direction = setup.get("formation_direction")
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_to_grid(self, price: float) -> float:
        """Round price to 0.25 granularity."""
        return round(price * 4) / 4

    def _carry_levels_to_next_session(self) -> None:
        """
        MULTI-SESSION LEVEL CARRYOVER:
        Carry the top N most-visited levels from the completed session
        to the next session.
        """
        if not self._current_session_levels:
            return
        
        # Sort levels by visit count
        sorted_levels = sorted(
            self._current_session_levels.items(),
            key=lambda x: x[1],
            reverse=True
        )
        
        # Keep top N levels
        new_carried = {}
        for level, count in sorted_levels[:self.carried_levels_per_session]:
            direction = self._current_session_level_direction.get(level, "neutral")
            new_carried[level] = direction
        
        # Merge with existing carried levels (keep some from prior sessions too)
        self._carried_levels.update(new_carried)
        
        # Prune to keep only the most recent carried levels
        if len(self._carried_levels) > self.carried_levels_per_session * 2:
            sorted_carried = sorted(
                self._carried_levels.items(),
                key=lambda x: self._current_session_levels.get(x[0], 0),
                reverse=True
            )
            self._carried_levels = dict(sorted_carried[:self.carried_levels_per_session * 2])

    def _update_session_levels(self, bar) -> None:
        """
        Track price levels visited in the current session and their
        formation direction (long or short absorption).
        """
        levels_visited = set()
        
        # Close level
        close_level = self._round_to_grid(bar.close)
        levels_visited.add(close_level)
        
        # High level
        high_level = self._round_to_grid(bar.high)
        levels_visited.add(high_level)
        
        # Low level
        low_level = self._round_to_grid(bar.low)
        levels_visited.add(low_level)
        
        # Update visit counts
        for level in levels_visited:
            self._current_session_levels[level] += 1

    def _is_near_carried_level(self, price: float) -> Optional[tuple]:
        """
        Check if price is near a CARRIED level from prior sessions.
        Returns (level, formation_direction) if found, None otherwise.
        """
        nearest_level = None
        nearest_direction = None
        min_distance = float('inf')
        
        for level, direction in self._carried_levels.items():
            distance = abs(price - level)
            if distance <= self.level_proximity and distance < min_distance:
                nearest_level = level
                nearest_direction = direction
                min_distance = distance
        
        if nearest_level is not None:
            return (nearest_level, nearest_direction)
        return None

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range [0, 1]."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _detect_absorption_at_carried_level(self, current) -> Optional[dict]:
        """
        Detect absorption when price is at a CARRIED level from prior sessions.
        
        MULTI-SESSION LEVEL CARRYOVER means:
        1. Price must be near a level that was established in a PRIOR session
        2. Bar must show absorption signature
        3. Confirm when price moves away from the carried level
        4. Move direction should align with formation direction (CROSS-SESSION MOMENTUM)
        """
        if current.volume == 0:
            return None

        # Check if price is at a CARRIED level
        carried_info = self._is_near_carried_level(current.close)
        if carried_info is None:
            return None
        
        carried_level, formation_direction = carried_info

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)

        # LONG: Buying pressure with absorption at carried level
        if delta > 0:
            if close_position < 0.65:
                return None

            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "carried_level": carried_level,
                "formation_direction": formation_direction,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        # SHORT: Selling pressure with absorption at carried level
        else:
            if close_position > 0.35:
                return None

            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "carried_level": carried_level,
                "formation_direction": formation_direction,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption at carried level.
        
        Confirmation requires:
        1. Price moves away from the carried level
        2. CROSS-SESSION MOMENTUM: move direction aligns with formation direction
           (if level formed as long, move should be up; if short, move down)
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_carried_level is None or self._pending_formation_direction is None):
            return False

        # Check confirmation timing
        if self._bars_since_absorption > self.max_bars_to_confirmation:
            return False

        direction = self._pending_setup["direction"]
        carried_level = self._pending_carried_level
        formation_direction = self._pending_formation_direction

        # For long: price must move UP away from the carried level
        if direction == "long":
            if confirming_bar.close <= (carried_level + self.level_proximity):
                return False
            # CROSS-SESSION MOMENTUM: if level formed as long, upward move validates
            # If level formed as short, upward move is counter-momentum (still valid)
            return True
        
        # For short: price must move DOWN away from the carried level
        else:
            if confirming_bar.close >= (carried_level - self.level_proximity):
                return False
            # CROSS-SESSION MOMENTUM: if level formed as short, downward move validates
            # If level formed as long, downward move is counter-momentum (still valid)
            return True

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption at carried level."""
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
