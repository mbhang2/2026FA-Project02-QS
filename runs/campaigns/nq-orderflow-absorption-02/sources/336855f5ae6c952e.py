"""
Volume profile absorption strategy - refined based on campaign analysis.

The current strategy produces identical trades to #76 on synthetic data,
indicating the core logic is sound. However, analysis of the best performers
reveals a critical insight:

Top performers:
- #81: -0.0096 with 622 trades (BEST SCORE by far)
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

#81 achieved a score 9x better than #76 with only 57% of the trades.
This suggests the key to improvement is NOT more trades, but HIGHER QUALITY
setups with better risk/reward.

Analysis of #81's likely parameters vs #76:
- Tighter delta_ratio (likely 0.35-0.40 vs #76's 0.28)
- Stricter level_proximity (likely 2.0-3.0 vs #76's 6.0)
- Higher min_wick_points (likely 0.25-0.50 vs #76's -0.5)
- Conservative target_multiple (likely 2.5-3.0 vs #76's 4.0)
- Smaller max_levels_per_session (likely 4-5 vs #76's 8)

The hypothesis: #81 succeeded by being MORE selective, not less. It enters
fewer setups but those setups are higher conviction - stronger flow, tighter
proximity to actual levels, better reward-to-risk ratios.

This trial implements a significantly stricter absorption detection:
1. Increase delta_ratio to 0.38 (very strong one-sided flow required)
2. Reduce level_proximity to 2.5 (must be very close to actual level)
3. Increase min_wick_points to 0.35 (clear evidence of rejection)
4. Reduce target_multiple to 2.8 (conservative but achievable)
5. Reduce max_levels_per_session to 5 (keep only highest volume levels)
6. Reduce max_stop_distance to 4.0 (avoid wide stops)

Expected: Score near #81's -0.0096 with 600-700 trades on real data.
The synthetic test should show fewer trades than current (70 → ~50-60).
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.38,
        level_proximity: float = 2.5,
        min_wick_points: float = 0.35,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.8,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_levels_per_session: int = 5,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
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
        Detect high-quality absorption with strict parameters.
        
        Key requirements for #81-level quality:
        - delta_ratio: 0.38 (very strong one-sided flow)
        - level_proximity: 2.5 (must be very close to actual level)
        - min_wick_points: 0.35 (clear price rejection evidence)
        - target_multiple: 2.8 (conservative reward)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Very strict threshold - only highest conviction absorption
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

        # Check proximity to carried level - must be very close
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
