"""
Volume profile absorption strategy - refined toward #81's -0.0096 score.

The byte-identical synthetic trades indicate the core logic is sound but the
parameters need adjustment to actually change behavior on real data.

Analysis of the gap between #76 (-0.0893, 1079 trades) and #81 (-0.0096, 622 trades):
- #81 trades 42% less frequently but scores 12x better
- This is a QUALITY difference, not a quantity one
- The previous trial's "recent volume" filter was too loose and didn't change synthetic behavior

Key insight: #81 likely succeeds through:
1. Much stricter delta_ratio (0.40+) - only the cleanest one-sided flow
2. Tighter level proximity (0.75) - must be RIGHT at the level, not near it
3. Higher min_tick_count (12) - requires real bar compression, not just activity
4. Stricter wick requirements - BOTH points and ratio matter
5. Tighter stop placement (0.10 buffer) - structural stops, not loose ones
6. Lower target_multiple (2.5) - conservative, achievable targets
7. Fewer top levels (3-4 per session) - only the absolute strongest levels matter
8. Stricter body ratio (0.50) - bars must be directional, not indecisive

The previous trial's recent_volume filter was a good idea but the implementation
was too lenient. This trial implements it more strictly: a level must have volume
in the CURRENT bar or the immediately preceding bar to be tradeable.

Expected: Different synthetic trades (not byte-identical), targeting #81's -0.0096
or better with 600-700 trades on real data.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.42,
        level_proximity: float = 0.75,
        min_wick_points: float = 0.40,
        min_wick_ratio: float = 0.35,
        min_body_ratio: float = 0.50,
        stop_buffer: float = 0.10,
        target_multiple: float = 2.5,
        max_stop_distance: float = 1.5,
        min_stop_distance: float = 0.35,
        size: int = 1,
        min_tick_count: int = 12,
        top_levels_per_session: int = 4,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.min_wick_ratio = min_wick_ratio
        self.min_body_ratio = min_body_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_tick_count = min_tick_count
        self.top_levels_per_session = top_levels_per_session

        # Level tracking
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        
        # Track previous bar for recent activity check
        self._prev_bar = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

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
            self._on_session_change(current.session)
            self._current_session = current.session
            self._prev_bar = None

        # Accumulate volume profile for current session
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

        # Store current bar as previous for next iteration
        self._prev_bar = current

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and carry forward."""
        if self._session_levels:
            levels = self._extract_top_levels(self._session_levels)
            self._prior_session_levels.append(levels)
            # Keep only last 2 sessions
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_bar_timestamps = set()
        self._prev_bar = None

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Use 0.25-point granularity
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _extract_top_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract only the strongest levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        
        # Take only top N levels - fewer, stronger levels
        top_levels = [level for level, _ in sorted_items[:self.top_levels_per_session]]
        
        return sorted(top_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        
        # Current session levels
        all_levels.extend(self._extract_top_levels(self._session_levels))
        
        # Prior session levels
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _level_has_recent_activity(self, level: float, current) -> bool:
        """Check if a level has volume in current or previous bar."""
        tolerance = 0.125  # Quarter of a level
        
        # Check current bar
        if current.low <= level <= current.high:
            return True
        
        # Check previous bar if it exists
        if self._prev_bar and self._prev_bar.low <= level <= self._prev_bar.high:
            return True
        
        return False

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption at strong carried levels.
        
        Much stricter than #76, targeting #81's quality:
        - delta_ratio >= 0.42 (very pure one-sided flow)
        - tick_count >= 12 (real compression)
        - Both wick AND ratio requirements
        - Body ratio >= 0.50 (directional bars only)
        - Level must have recent activity
        - Tight proximity (0.75)
        - Tight stops (0.10 buffer, 1.5 max distance)
        - Conservative targets (2.5x risk)
        """
        if current.volume == 0:
            return None

        # Require significant bar activity
        if current.tick_count < self.min_tick_count:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Require directional bar
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range
        if body_ratio < self.min_body_ratio:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Strict one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Get carried levels
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        # Find closest level that is:
        # 1. Within proximity
        # 2. Has recent activity
        best_level = None
        min_distance = float('inf')
        
        for level in carried_levels:
            distance = abs(current.close - level)
            if distance <= self.level_proximity:
                if self._level_has_recent_activity(level, current):
                    if distance < min_distance:
                        best_level = level
                        min_distance = distance

        if best_level is None:
            return None

        # Long setup
        if delta > 0:
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range

            # Require BOTH absolute wick size AND ratio
            if lower_wick < self.min_wick_points or wick_ratio < self.min_wick_ratio:
                return None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        # Short setup
        else:
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range

            # Require BOTH absolute wick size AND ratio
            if upper_wick < self.min_wick_points or wick_ratio < self.min_wick_ratio:
                return None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on absorption detection."""
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
