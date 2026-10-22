"""
Volume profile absorption strategy - refined based on #81's exceptional performance.

Campaign analysis shows #81 achieved -0.0096 with 622 trades - exceptional performance.
The synthetic test confirms the core logic executes correctly (70 identical trades).

Key insight: The synthetic data is too sparse to differentiate parameter changes.
We need to focus on what #81's parameters likely were and push toward them more
aggressively. Analysis of the score distribution suggests:

- #81: -0.0096 (622 trades) - BEST
- #86: -0.0600 (673 trades)
- #92: -0.1250 (517 trades)
- #77: -0.1518 (692 trades)

#81's exceptional score with moderate trade frequency suggests it achieved the
best quality-to-frequency ratio. The key differences likely were:

1. STRICTER level proximity: 1.5-2.0 (much tighter to actual carried levels)
2. HIGHER delta ratio: 0.34-0.36 (only strongest one-sided flow)
3. HIGHER wick requirement: 0.35-0.45 (clear price rejection)
4. LOWER target multiple: 2.5-2.7 (realistic, achievable targets)
5. FEWER levels per session: 4 (only the most significant)
6. Possibly LOWER min_abs_delta: 30-40 (not too restrictive)

The critical realization: We should also consider that #81 might have had a
different approach to level extraction - perhaps using a percentile-based method
rather than just taking the top N levels. Or it might have required a minimum
volume threshold for levels to be considered.

This trial implements #81-oriented parameters with aggressive tightening:
- delta_ratio: 0.34 (very strict one-sided flow)
- level_proximity: 1.8 (much tighter to levels)
- min_wick_points: 0.35 (strong rejection signal)
- target_multiple: 2.6 (conservative targets)
- max_levels_per_session: 4 (only strongest)
- max_stop_distance: 3.2 (tight risk control)
- min_abs_delta: 40 (genuine volume conviction)

Expected: Score in -0.01 to -0.05 range with 500-700 trades on real data.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.34,
        min_abs_delta: int = 40,
        level_proximity: float = 1.8,
        min_wick_points: float = 0.35,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.6,
        max_stop_distance: float = 3.2,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_levels_per_session: int = 4,
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

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        
        # Track bars we've entered on to avoid re-entry
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
            # Keep only last 2 sessions of levels
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
        Detect high-quality absorption at carried volume levels.
        
        Tuned toward #81's exceptional performance:
        - delta_ratio: 0.34 (very strict one-sided flow)
        - min_abs_delta: 40 (genuine volume conviction)
        - level_proximity: 1.8 (tight to actual levels)
        - min_wick_points: 0.35 (strong price rejection)
        - target_multiple: 2.6 (conservative reward/risk)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        
        # Require genuine volume conviction
        if abs(delta) < self.min_abs_delta:
            return None
        
        delta_ratio = abs(delta) / current.volume

        # Very strict ratio threshold - only strongest one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require strong wick showing price rejection
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

        # Check proximity to carried level - must be tight
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
