"""
Volume profile absorption strategy - refinement targeting improvement over #76's -0.0893.

Analysis of the plateau and recent results:
- #76 scored -0.0893 with 1079 trades (best score)
- #74 scored -0.0980 with 2763 trades
- #16 scored -0.1076 with 463 trades
- Synthetic baseline: consistently 70 trades (random noise without structure)

The synthetic invariant reveals that synthetic data lacks real volume structure.
The 70-trade floor is the natural output when absorption detection runs on noise.

Key insight from #76's success: The sweet spot is around 1000-1100 trades with
good selectivity. #76 likely had parameters that were:
1. Moderately strict on delta_ratio (around 0.32-0.35)
2. Moderate proximity threshold (around 3.0-3.5 points)
3. Relaxed wick requirement (around 0.0-0.05)
4. Reasonable target multiple (3.0-3.2)

The current strategy produces 70 trades on synthetic, which is correct. But the
real data needs better calibration. The issue is that the min_level_volume
filter of 200 may be too loose, creating noise levels. Meanwhile, the wick
requirement of 0.05 may be too strict.

This trial refines:
1. Increase min_level_volume to 400 - be more selective about which levels matter
2. Reduce min_wick_points to 0.0 - absorption can occur without opposing wick
3. Tighten delta_ratio to 0.32 - require stronger one-sided flow
4. Tighten level_proximity to 3.0 - price must be closer to level
5. Reduce target_multiple to 3.0 - more conservative reward targets
6. Reduce max_stop_distance to 4.0 - avoid wide stops

The hypothesis: #76 achieved 1079 trades with this kind of balanced selectivity.
This trial replicates that balance more precisely.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.32,
        level_proximity: float = 3.0,
        min_wick_points: float = 0.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_level_volume: int = 400,
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
        self.min_level_volume = min_level_volume

        # Level tracking
        self._baseline_levels: dict[float, int] = {}
        self._session_levels: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._prior_session_levels: list[dict[float, int]] = []
        
        # Track bars we've entered on to prevent re-entry
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Build baseline levels from training split
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars:
                self._baseline_levels = self._build_profile(train_bars)
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

        # Accumulate volume profile for current session
        self._add_bar_to_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract and carry levels from completed session."""
        if self._session_levels:
            self._prior_session_levels.append(dict(self._session_levels))
            # Keep last 2 sessions
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        self._session_levels = {}
        self._entered_bar_timestamps = set()

    def _build_profile(self, bars) -> dict[float, int]:
        """Build volume profile from bars."""
        profile: dict[float, int] = defaultdict(int)
        
        for bar in bars:
            if bar.volume <= 0 or bar.high <= bar.low:
                continue
            
            low = round(bar.low * 4) / 4
            high = round(bar.high * 4) / 4
            
            num_levels = max(1, int((high - low) / 0.25) + 1)
            vol_per_level = bar.volume / num_levels
            
            level = low
            while level <= high + 0.01:
                level_key = round(level * 4) / 4
                profile[level_key] += int(vol_per_level)
                level += 0.25
        
        return dict(profile)

    def _add_bar_to_profile(self, bar) -> None:
        """Add bar to current session profile."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        low = round(bar.low * 4) / 4
        high = round(bar.high * 4) / 4
        
        num_levels = max(1, int((high - low) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels
        
        level = low
        while level <= high + 0.01:
            level_key = round(level * 4) / 4
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _get_significant_levels(self) -> list[float]:
        """Get all significant levels with minimum volume threshold."""
        all_levels: dict[float, int] = {}
        
        # Current session levels
        for level, vol in self._session_levels.items():
            all_levels[level] = all_levels.get(level, 0) + vol
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            for level, vol in session_profile.items():
                all_levels[level] = all_levels.get(level, 0) + vol
        
        # Baseline levels
        for level, vol in self._baseline_levels.items():
            all_levels[level] = all_levels.get(level, 0) + vol
        
        # Filter for significant levels (minimum volume)
        significant = [
            level for level, vol in all_levels.items()
            if vol >= self.min_level_volume
        ]
        
        return sorted(significant)

    def _find_nearest_level(self, price: float) -> Optional[float]:
        """Find nearest significant level within proximity threshold."""
        levels = self._get_significant_levels()
        if not levels:
            return None
        
        nearest = min(levels, key=lambda l: abs(l - price))
        distance = abs(nearest - price)
        
        if distance <= self.level_proximity:
            return nearest
        
        return None

    def _detect_absorption(self, bar) -> Optional[dict]:
        """
        Detect absorption:
        - Strong one-sided delta at a carried level
        - Optional opposing wick (relaxed requirement)
        - Valid stop/target distances
        """
        if bar.volume == 0:
            return None

        # Must be at a carried level
        nearest_level = self._find_nearest_level(bar.close)
        if nearest_level is None:
            return None

        delta = bar.delta
        delta_ratio = abs(delta) / bar.volume

        # Delta requirement - tighter selectivity
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for wick opposite to flow (relaxed - can be zero)
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

        # Calculate stop and validate
        if delta > 0:  # Long
            stop_price = bar.low - self.stop_buffer
            stop_distance = bar.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            target_distance = stop_distance * self.target_multiple
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }
        
        else:  # Short
            stop_price = bar.high + self.stop_buffer
            stop_distance = stop_price - bar.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            target_distance = stop_distance * self.target_multiple
            
            return {
                "direction": "short",
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
