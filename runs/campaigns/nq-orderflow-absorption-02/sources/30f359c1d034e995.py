"""
Volume profile absorption strategy - refined to improve on #74's -0.0980 score.

Key observations from the campaign:
- #74 scored -0.0980 with 2763 trades (best real score)
- #16 scored -0.1076 with 463 trades
- Synthetic baseline consistently produces ~70 trades
- The campaign asks about absorption at carried levels preceding moves between levels

Critical refinement for this trial:
1. The previous strategy's level extraction may be too aggressive (top 70% of volume)
   - This creates too many levels and reduces discrimination
   - Tighten to top 50% of volume for more significant levels
   
2. Level proximity check at 1.0 point may be too strict
   - Synthetic data has no volume structure, explaining 70 vs 2763 gap
   - Increase to 2.0 points for better detection on real data
   
3. The target distance calculation to "next level" may be creating unrealistic targets
   - If next level is very far, the target_offset becomes huge
   - Cap target_distance to a multiple of stop_distance (e.g., 3.5x)
   - This matches the campaign's guidance on reward-to-risk multiples
   
4. Session boundary handling: levels should accumulate across sessions more carefully
   - Keep last 3 sessions instead of 2
   - Extract levels more conservatively (top 5-6 levels, not 70% threshold)

The hypothesis: #74 found a sweet spot with 2763 trades. This trial makes small
adjustments to the level detection and target calculation to improve precision
while maintaining frequency. The key change is using a capped multiple-based
target instead of distance-to-next-level, which may have been creating
unrealistic targets on sparse level distributions.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        min_wick_points: float = 0.0,
        stop_buffer: float = 0.25,
        size: int = 1,
        level_proximity: float = 2.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        target_multiple: float = 3.5,
        level_volume_percentile: float = 0.50,
        max_levels_to_keep: int = 6,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.size = size
        self.level_proximity = level_proximity
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.target_multiple = target_multiple
        self.level_volume_percentile = level_volume_percentile
        self.max_levels_to_keep = max_levels_to_keep

        # Session-based level tracking
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []  # Last 3 sessions
        
        # Entries tracking - prevent re-entry on same level in same session
        self._entered_at_levels: set[float] = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current = history[-1]

        # Session boundary handling
        if current.session != self._current_session:
            self._on_session_boundary(current.session)
            self._current_session = current.session

        # Accumulate volume profile for current session
        self._add_to_profile(current)

        # Skip if in position
        if ctx.position or ctx.working_order_ids:
            return

        # Detect absorption at a carried level
        setup = self._detect_absorption_at_level(current)
        if setup:
            self._entered_at_levels.add(setup["entry_level"])
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_boundary(self, new_session: int) -> None:
        """Handle session transitions."""
        # Extract and carry forward levels from completed session
        if self._session_levels:
            levels = self._extract_levels_from_profile()
            self._prior_session_levels.append(levels)
            # Keep only last 3 sessions
            if len(self._prior_session_levels) > 3:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_at_levels = set()

    def _add_to_profile(self, bar) -> None:
        """Add bar's volume to price profile."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Distribute volume across 0.25-point levels
        low = round(bar.low * 4) / 4
        high = round(bar.high * 4) / 4
        
        num_levels = max(1, int((high - low) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels
        
        level = low
        while level <= high + 0.01:
            level_key = round(level * 4) / 4
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _extract_levels_from_profile(self) -> list[float]:
        """Extract significant levels from current session profile."""
        if not self._session_levels:
            return []
        
        sorted_items = sorted(self._session_levels.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)
        
        if total_vol == 0:
            return []
        
        # Get top levels up to max_levels_to_keep that comprise the percentile
        levels = []
        cumulative = 0
        for level, vol in sorted_items:
            levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * self.level_volume_percentile or len(levels) >= self.max_levels_to_keep:
                break
        
        return sorted(levels)

    def _get_all_carried_levels(self) -> list[float]:
        """Get all levels carried from current and prior sessions."""
        all_levels = []
        
        # Current session levels
        all_levels.extend(self._extract_levels_from_profile())
        
        # Prior session levels
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _find_nearest_level(self, price: float) -> Optional[float]:
        """Find carried level nearest to price within proximity threshold."""
        levels = self._get_all_carried_levels()
        if not levels:
            return None
        
        nearest = min(levels, key=lambda l: abs(l - price))
        distance = abs(nearest - price)
        
        if distance <= self.level_proximity:
            return nearest
        
        return None

    def _detect_absorption_at_level(self, bar) -> Optional[dict]:
        """
        Detect absorption: strong delta at a carried level with opposing wick.
        Entry at the level, target as multiple of stop distance.
        """
        if bar.volume == 0:
            return None

        # Find if price is near a carried level
        nearest_level = self._find_nearest_level(bar.close)
        if nearest_level is None:
            return None
        
        # Already entered at this level in current session
        if nearest_level in self._entered_at_levels:
            return None

        delta = bar.delta
        delta_ratio = abs(delta) / bar.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for wick opposite to flow
        has_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = bar.close - bar.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = bar.high - bar.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Determine direction and calculate stop
        if delta > 0:  # Long absorption
            stop_price = bar.low - self.stop_buffer
            stop_distance = bar.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Target is multiple of stop distance
            target_distance = stop_distance * self.target_multiple
            
            return {
                "direction": "long",
                "entry_level": nearest_level,
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }
        
        else:  # Short absorption
            stop_price = bar.high + self.stop_buffer
            stop_distance = stop_price - bar.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Target is multiple of stop distance
            target_distance = stop_distance * self.target_multiple
            
            return {
                "direction": "short",
                "entry_level": nearest_level,
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order."""
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
