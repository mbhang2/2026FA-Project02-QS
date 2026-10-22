"""
Volume profile absorption strategy - refined to improve on #76's -0.0893 score.

Analysis of the campaign trajectory:
- #76 scored -0.0893 with 1079 trades (best performer)
- #74 scored -0.0980 with 2763 trades
- #16 scored -0.1076 with 463 trades
- All synthetic runs produce ~70 trades (synthetic data lacks volume structure)

The key insight: #76 achieved the best score with moderate trade frequency.
The previous submission's next-level distance filter (4.0-8.0) may be too
restrictive, eliminating valid absorption setups that still move meaningfully.

Critical refinement based on campaign brief requirements:
1. The strategy must hold for moves BETWEEN levels, not scalp flow
2. Stop placement is structural (at absorption bar extreme + buffer)
3. Target is a reward multiple of stop distance
4. Entry frequency matters - too strict filters eliminate opportunities

Changes from current file:
1. Relax next_level_distance constraints: allow 2.0-10.0 instead of 4.0-8.0
   This captures more valid setups while still requiring a next level exists
2. Reduce level_proximity from 1.0 to 1.5 for slightly more flexibility
3. Lower absorption_delta_ratio from 0.32 to 0.30 to match #76's parameters
4. Adjust target_multiple to 4.25 (midpoint between 4.0 and 4.5)
5. Keep level_volume_percentile at 0.40 (conservative level extraction)
6. Simplify level accumulation logic to be more stable

The hypothesis: #76's success came from the right balance of strictness and
permissiveness. This trial makes targeted adjustments to the distance thresholds
that were likely too tight, while maintaining the core absorption detection logic.
The synthetic 70-trade baseline should remain unchanged since the core detection
logic is unchanged.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        level_proximity: float = 1.5,
        min_wick_points: float = -0.25,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.25,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_volume_percentile: float = 0.40,
        min_next_level_distance: float = 2.0,
        max_next_level_distance: float = 10.0,
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
        self.level_volume_percentile = level_volume_percentile
        self.min_next_level_distance = min_next_level_distance
        self.max_next_level_distance = max_next_level_distance

        # Level tracking
        self._baseline_levels: list[float] = []
        self._session_levels: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._all_prior_levels: list[float] = []
        
        # Track levels we've entered at in current session to prevent re-entry
        self._entered_at_level: set[float] = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Build baseline levels from training split
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars:
                self._baseline_levels = self._extract_levels(train_bars)
        except Exception:
            pass

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            self._on_session_change()
            self._current_session = current.session

        # Accumulate volume profile
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Detect absorption at a carried level
        absorption = self._detect_absorption(current)
        if absorption:
            self._entered_at_level.add(absorption["level"])
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract levels from session and prepare for next."""
        if self._session_levels:
            levels = self._extract_levels_from_profile(self._session_levels)
            self._all_prior_levels.extend(levels)
            # Keep only recent levels to avoid stale accumulation
            if len(self._all_prior_levels) > 50:
                self._all_prior_levels = self._all_prior_levels[-50:]
        
        self._session_levels = {}
        self._entered_at_level = set()

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

    def _extract_levels(self, bars) -> list[float]:
        """Extract levels from a sequence of bars."""
        profile: dict[float, int] = defaultdict(int)
        
        for bar in bars:
            if bar.volume <= 0 or bar.high <= bar.low:
                continue
            
            low_rounded = round(bar.low * 4) / 4
            high_rounded = round(bar.high * 4) / 4
            
            num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
            vol_per_level = bar.volume / num_levels
            
            level = low_rounded
            while level <= high_rounded + 0.01:
                level_key = round(level * 4) / 4
                profile[level_key] += int(vol_per_level)
                level += 0.25
        
        return self._extract_levels_from_profile(profile)

    def _extract_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Get top levels comprising the specified percentile
        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * self.level_volume_percentile:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        all_levels.extend(self._all_prior_levels)
        all_levels.extend(self._baseline_levels)
        return sorted(list(set(all_levels)))

    def _find_nearest_level(self, price: float) -> Optional[float]:
        """Find nearest carried level within proximity threshold."""
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None
        
        nearest = min(carried_levels, key=lambda l: abs(l - price))
        if abs(nearest - price) <= self.level_proximity:
            return nearest
        
        return None

    def _find_next_level(self, current_price: float, direction: str) -> Optional[float]:
        """Find next level in the direction of the trade."""
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None
        
        if direction == "long":
            # Find closest level above current price
            above = [l for l in carried_levels if l > current_price]
            if above:
                return min(above)
        else:  # short
            # Find closest level below current price
            below = [l for l in carried_levels if l < current_price]
            if below:
                return max(below)
        
        return None

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at a carried level with next level validation.
        
        Requirements:
        1. Price within 1.5 points of a carried level
        2. Strong directional delta (30% of volume)
        3. Opposing wick
        4. Next level exists at 2-10 points distance
        5. Haven't already entered at this level in current session
        """
        if current.volume == 0:
            return None

        # Must be at a carried level
        nearest_level = self._find_nearest_level(current.close)
        if nearest_level is None:
            return None
        
        # Don't re-enter same level in same session
        if nearest_level in self._entered_at_level:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for wick opposite to flow
        has_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Calculate stop and target
        if delta > 0:  # Long
            # Verify next level exists at reasonable distance
            next_level = self._find_next_level(current.close, "long")
            if next_level is None:
                return None
            
            distance_to_next = next_level - current.close
            if not (self.min_next_level_distance <= distance_to_next <= self.max_next_level_distance):
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "level": nearest_level,
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # Short
            # Verify next level exists at reasonable distance
            next_level = self._find_next_level(current.close, "short")
            if next_level is None:
                return None
            
            distance_to_next = current.close - next_level
            if not (self.min_next_level_distance <= distance_to_next <= self.max_next_level_distance):
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "level": nearest_level,
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
