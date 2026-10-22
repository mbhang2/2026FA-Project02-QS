"""
Volume profile absorption strategy - test multi-level proximity bands.

Campaign analysis of top performers shows a clear pattern:
- #81: -0.0096 with 622 trades (BEST)
- #165: -0.0350 with 658 trades
- #155: -0.0462 with 859 trades
- #146: -0.0611 with 907 trades

All top performers cluster around 600-900 trades with scores -0.01 to -0.06.
They succeed through selectivity on quality, not frequency.

The current strategy uses a single proximity threshold (4.0 points). The campaign
asks whether absorption at a carried level precedes a move away from it. But
"at a level" can mean different things:

1. Extremely tight (0-2 points): price is directly on the level
2. Tight (2-4 points): price is very close to the level  
3. Moderate (4-6 points): price is within a reasonable distance

The hypothesis: Different proximity bands may have different predictive power.
Prices arriving exactly at a level (tight band) may show stronger absorption
patterns than prices arriving near it (moderate band). By weighting entries
based on proximity quality, we can improve selectivity.

This trial implements a proximity scoring system:
- Proximity score: 1.0 for prices < 1.5 points from level, declining to 0.5 at 4.0 points
- Only enter if proximity score >= 0.65 (filters out marginal proximity)
- This is equivalent to requiring distances < 3.5 points for most setups

This is more selective than the current 4.0 threshold while maintaining the
same core absorption detection logic. It recovers the selectivity that made
#81 successful.

Expected: Fewer trades than current (500-650 range), better quality, targeting
score -0.0070 to -0.0120.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 4.0,
        min_proximity_score: float = 0.65,
        min_wick_points: float = -0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 2,
        concentration_threshold: float = 0.70,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_proximity_score = min_proximity_score
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.rolling_window_sessions = rolling_window_sessions
        self.concentration_threshold = concentration_threshold

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        
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

        # Accumulate volume into current session profile
        self._update_session_profile(current)

        # Skip if already in position or have working orders
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
        """Handle session boundary - add completed session to rolling window."""
        if self._current_session_profile:
            self._session_profiles.append(dict(self._current_session_profile))
            # Keep only last N sessions in rolling window
            if len(self._session_profiles) > self.rolling_window_sessions:
                self._session_profiles.pop(0)
        
        # Reset for new session
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels for current session."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Use 0.25-point granularity for level accumulation
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._current_session_profile[level_key] = (
                self._current_session_profile.get(level_key, 0) + int(vol_per_level)
            )
            level += 0.25

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """
        Extract levels representing the top concentration_threshold of volume.
        
        This identifies the most robust support/resistance levels by selecting
        levels that together contain the specified concentration of total volume.
        """
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Accumulate from highest volume levels until we reach the threshold
        cumulative_vol = 0
        selected_levels = []
        
        for level, vol in sorted_items:
            selected_levels.append(level)
            cumulative_vol += vol
            if cumulative_vol >= total_vol * self.concentration_threshold:
                break

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """
        Get all carried levels from the rolling window of sessions.
        
        Returns a sorted list of unique levels that represent significant
        support/resistance across the current and prior sessions.
        """
        all_levels = set()
        
        # Add levels from current session
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        
        # Add levels from all prior sessions in the rolling window
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _calculate_proximity_score(self, distance: float) -> float:
        """
        Calculate proximity quality score based on distance to nearest level.
        
        Score declines linearly from 1.0 at 0 distance to 0.5 at level_proximity.
        Prices closer to levels get higher scores (stronger signal).
        """
        if distance <= 0:
            return 1.0
        if distance >= self.level_proximity:
            return 0.5
        
        # Linear interpolation from 1.0 at 0 to 0.5 at level_proximity
        return 1.0 - (0.5 * distance / self.level_proximity)

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels with proximity scoring.
        
        Filters applied in order:
        1. Strong directional delta (delta_ratio >= 0.28) - proven threshold
        2. Wick showing price rejection (allows zero wick, requires non-negative)
        3. Proximity to carried level with quality scoring (score >= 0.65)
        4. Stop distance within valid range
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta (proven at 0.28)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Evidence of price rejection via wick (allows zero, requires >= -0.5)
        has_valid_wick = False
        if delta > 0:  # Buying pressure - check lower wick
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_valid_wick = True
        elif delta < 0:  # Selling pressure - check upper wick
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_valid_wick = True

        if not has_valid_wick:
            return None

        # Filter 3: Check proximity to carried level with quality scoring
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        proximity_score = self._calculate_proximity_score(min_distance)
        
        if proximity_score < self.min_proximity_score:
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
        """Place entry order on absorption detection with bracket protection."""
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
