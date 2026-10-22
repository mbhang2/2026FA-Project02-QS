"""
Volume profile absorption strategy - refining #81's -0.0096 by improving level extraction.

The synthetic test showed byte-identical trades to the current strategy. Analysis suggests
the issue is not in the absorption detection logic itself, but in how levels are extracted
and carried forward from the volume profile.

Current approach extracts top 70% of volume by cumulative sum, which may miss important
structural price levels that have moderate volume but high significance for price action.

Key insight from #81's success (score -0.0096, 622 trades):
- It traded moderately frequently but with high quality
- The level proximity check (3.5 points) was effective
- The delta_ratio (0.28) and target_multiple (4.0) were proven

Critical change: Instead of extracting levels by cumulative volume percentage,
extract the TOP N most-traded price levels explicitly. This ensures we capture
the actual points of control and value areas, not just cumulative thresholds.

Additional refinements:
1. Extract top 12 price levels per session (most significant nodes)
2. Keep stricter delta_ratio: 0.28 (proven by #81)
3. Keep wick requirement: 0.35 (rejection signal)
4. Keep level_proximity: 3.0 (tight to level)
5. Keep target_multiple: 4.0 (proven)
6. Add body_ratio check: >= 0.40 (directional commitment)
7. Add tick_count check: >= 6 (real bar, not noise)
8. Keep volume context filter for real activity

The hypothesis: By extracting the top 12 explicit levels instead of cumulative
percentage, we get more precise level identification that better represents where
actual trading occurred, improving the absorption detection accuracy.

Expected: Different synthetic behavior (not byte-identical) due to different level
extraction method, targeting #81's -0.0096 or better through improved level quality.
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
        level_proximity: float = 3.0,
        min_wick_ratio: float = 0.35,
        min_body_ratio: float = 0.40,
        min_tick_count: int = 6,
        min_bar_range: float = 1.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        top_levels_per_session: int = 12,
        volume_window: int = 20,
        volume_ratio_threshold: float = 0.80,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_ratio = min_wick_ratio
        self.min_body_ratio = min_body_ratio
        self.min_tick_count = min_tick_count
        self.min_bar_range = min_bar_range
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.top_levels_per_session = top_levels_per_session
        self.volume_window = volume_window
        self.volume_ratio_threshold = volume_ratio_threshold

        # Level tracking
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        
        # Volume context tracking
        self._session_volumes: deque = deque(maxlen=volume_window)
        
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
            self._session_volumes = deque(maxlen=self.volume_window)

        # Accumulate volume profile
        self._update_session_profile(current)
        
        # Track volume for context
        self._session_volumes.append(current.volume)

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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and carry forward."""
        if self._session_levels:
            levels = self._extract_top_levels(self._session_levels)
            self._prior_session_levels.append(levels)
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        self._session_levels = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels with 0.25-point granularity."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

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
        """Extract the top N most-traded price levels explicitly."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        
        # Take the top N levels by volume, not by cumulative percentage
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

    def _get_volume_median(self) -> float:
        """Get median volume from session window."""
        if not self._session_volumes:
            return 0.0
        sorted_vols = sorted(list(self._session_volumes))
        return float(sorted_vols[len(sorted_vols) // 2])

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with improved level extraction.
        
        Filters (in order):
        1. Bar range >= 1.0 (need real price movement)
        2. Tick count >= 6 (real bar activity)
        3. Body ratio >= 0.40 (directional commitment)
        4. Delta ratio >= 0.28 (strong one-sided flow)
        5. Wick in opposite direction >= 0.35 (rejection signal)
        6. Volume >= 80% of session median (real activity)
        7. Proximity to carried level <= 3.0 (tight to level)
        """
        if current.volume == 0:
            return None

        bar_range = current.high - current.low
        
        # Check 1: Minimum bar range
        if bar_range < self.min_bar_range:
            return None

        # Check 2: Tick count (real activity)
        if current.tick_count < self.min_tick_count:
            return None

        # Check 3: Body ratio (directional commitment)
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range if bar_range > 0 else 0
        if body_ratio < self.min_body_ratio:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Check 4: Strong directional flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check 5: Wick in opposite direction
        if delta > 0:  # Long: buying pressure, need lower wick
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range
            
            if wick_ratio < self.min_wick_ratio:
                return None
        
        elif delta < 0:  # Short: selling pressure, need upper wick
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range
            
            if wick_ratio < self.min_wick_ratio:
                return None
        
        else:
            return None

        # Check 6: Volume context
        volume_median = self._get_volume_median()
        if volume_median > 0:
            if current.volume < volume_median * self.volume_ratio_threshold:
                return None

        # Check 7: Proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Calculate stop and target
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # Short
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
