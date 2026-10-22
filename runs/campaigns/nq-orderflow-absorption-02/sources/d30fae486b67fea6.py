"""
Volume profile absorption strategy - refined toward #81's -0.0096 performance.

Campaign analysis reveals the pattern:
- #81: -0.0096 with 622 trades (EXCEPTIONAL - 10x better than #76)
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

The synthetic test shows 70 identical trades to the current strategy, meaning
the core logic executes but parameters need adjustment to reach #81's quality.

Critical realization: The synthetic test is producing IDENTICAL trades because
the parameters are still too loose. The synthetic data has only 70 total trades,
yet we're generating 70 entries - meaning we're entering on almost every single
trade event. This suggests our filters are not actually filtering.

The problem: Our delta_ratio threshold is being checked against bar.delta,
but in synthetic data with sparse trades, bars are small and delta_ratio can
appear high even on weak signals. We need to also filter on ABSOLUTE delta
magnitude, not just the ratio.

Additionally, the level_proximity check may be failing silently because we have
no carried levels in early sessions. We should only trade when we have a robust
set of carried levels to reference.

Refined approach:
1. Add absolute delta minimum: abs(delta) >= 50 contracts (real conviction)
2. Require minimum carried levels: at least 3 levels from prior sessions
3. Increase delta_ratio to 0.35 (stricter ratio-based filtering)
4. Keep level_proximity at 2.5 (tight to actual levels)
5. Increase min_wick_points to 0.40 (stronger price rejection)
6. Decrease target_multiple to 2.5 (conservative, realistic targets)
7. Decrease max_levels_per_session to 4 (only strongest levels)
8. Decrease max_stop_distance to 3.0 (tight risk bounds)

The key change: Adding absolute delta filter means we only trade when there's
genuine volume conviction, not just a favorable ratio on a small bar. This
should dramatically reduce synthetic trades from 70 to something much lower
(perhaps 5-10), indicating real selectivity.

Expected: Score near #81's -0.0096 with 400-600 trades on real data.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        min_abs_delta: int = 50,
        level_proximity: float = 2.5,
        min_wick_points: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 3.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_levels_per_session: int = 4,
        min_carried_levels: int = 3,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_abs_delta = min_abs_delta
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.max_levels_per_session = max_levels_per_session
        self.min_carried_levels = min_carried_levels

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        
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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and carry forward."""
        if self._session_levels:
            levels = self._extract_levels_from_profile(self._session_levels)
            self._prior_session_levels.append(levels)
            # Keep only last 2 sessions
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Distribute volume across 0.25-point granularity
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _extract_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract the most significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)

        # Keep only top N most significant levels
        top_levels = [level for level, vol in sorted_items[:self.max_levels_per_session]]

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        
        # Current session levels
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        
        # Prior session levels
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption with strict selectivity.
        
        Key filters:
        - Absolute delta minimum: must have genuine volume conviction
        - Delta ratio: 0.35 (stricter one-sided flow)
        - Level proximity: 2.5 (tighter to actual levels)
        - Min wick: 0.40 (strong price rejection)
        - Require carried levels: only trade when we have reference points
        """
        if current.volume == 0:
            return None

        delta = current.delta
        
        # CRITICAL: Require absolute delta magnitude (genuine conviction)
        if abs(delta) < self.min_abs_delta:
            return None
        
        delta_ratio = abs(delta) / current.volume

        # Stricter ratio threshold
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require clear wick showing price rejection
        has_wick = False
        if delta > 0:  # Buying pressure
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to carried level - must have reference levels
        carried_levels = self._get_carried_levels()
        if len(carried_levels) < self.min_carried_levels:
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
