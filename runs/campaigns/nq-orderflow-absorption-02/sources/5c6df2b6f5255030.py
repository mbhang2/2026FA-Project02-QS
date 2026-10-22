"""
Volume profile absorption strategy - refinement targeting score improvement.

Analysis of recent trials:
- #74 (best real score): -0.0980 with 2763 trades
- #72: -0.1439 with 2331 trades  
- #73: -0.2461 with 12150 trades (too many trades, poor quality)

The pattern: moderate trade frequency (2300-2800) performs better than high frequency.
#74's success came from relaxed thresholds (delta_ratio=0.20, proximity=8.0).

Key observations:
1. The synthetic baseline consistently produces 70 trades regardless of parameters
2. This suggests the synthetic data lacks the volume profile structure needed for
   absorption detection to work - it's random noise
3. Real data shows absorption detection works (2700+ trades on #74)
4. The issue is NOT detection strictness but ENTRY QUALITY

New approach based on campaign brief:
- The brief emphasizes holding for moves BETWEEN levels, not trading the flow itself
- Previous attempts traded too frequently (high cost relative to moves)
- #74 was successful but could be refined by:
  1. Keeping the relaxed absorption detection (#74's delta_ratio=0.20, proximity=8.0)
  2. Adding a HOLDING PERIOD filter: only enter if we can hold to next level
  3. Computing expected distance to next level, requiring it to be substantial
  4. This reduces entry frequency while maintaining quality

The key insight: Don't just detect absorption, ensure there's a meaningful move
to capture. A level 8.0 points away is not useful if the next level is only 2.0
points further. Filter for setups where we have room to move between levels.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.20,
        level_proximity: float = 8.0,
        min_wick_points: float = -1.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_level_spacing: float = 4.0,
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
        self.min_level_spacing = min_level_spacing

        # Level tracking
        self._baseline_levels: list[float] = []
        self._session_levels: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._all_prior_levels: list[float] = []
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

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

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        absorption = self._detect_absorption(current)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
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
        
        self._session_levels = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
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

        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.70:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        all_levels.extend(self._all_prior_levels)
        all_levels.extend(self._baseline_levels)
        return sorted(list(set(all_levels)))

    def _find_next_level(self, current_price: float, direction: str, carried_levels: list[float]) -> Optional[float]:
        """Find the next level in the direction of the trade."""
        if not carried_levels:
            return None
        
        if direction == "long":
            # Find the closest level above current price
            above = [l for l in carried_levels if l > current_price]
            if above:
                return min(above)
        else:  # short
            # Find the closest level below current price
            below = [l for l in carried_levels if l < current_price]
            if below:
                return max(below)
        
        return None

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with level-spacing filter.
        Only enter if there's a meaningful next level to target.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        if delta_ratio < self.absorption_delta_ratio:
            return None

        has_wick = False
        if delta > 0:
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # NEW: Check that there's a meaningful next level
        if delta > 0:  # Long
            next_level = self._find_next_level(current.close, "long", carried_levels)
            if next_level is None:
                return None
            # Distance to next level should be substantial relative to stop distance
            distance_to_next = next_level - current.close
            if distance_to_next < self.min_level_spacing:
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
        
        else:  # Short
            next_level = self._find_next_level(current.close, "short", carried_levels)
            if next_level is None:
                return None
            # Distance to next level should be substantial
            distance_to_next = current.close - next_level
            if distance_to_next < self.min_level_spacing:
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
