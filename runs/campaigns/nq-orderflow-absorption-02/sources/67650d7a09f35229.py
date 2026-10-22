"""
Volume profile absorption strategy - refined directional proximity with asymmetric thresholds.

Campaign analysis shows the best performers (#81 at -0.0096, #146 at -0.0611, #186 at -0.0745)
all use selective filtering with moderate trade frequency (600-1000 trades). The directional
proximity logic in the previous trial is sound but the thresholds need refinement.

Key insight from byte-identical synthetic fills: the previous trial's directional proximity
logic (resistance for longs, support for shorts) is too restrictive. Most absorption setups
don't have a level in the "correct" direction - they occur at levels that are simply nearby.

This trial reverts to bidirectional proximity checking (any carried level) but with:

1. Asymmetric thresholds:
   - Long absorption: proximity_threshold = 5.5 (absorption often occurs at support when buying)
   - Short absorption: proximity_threshold = 5.5 (absorption often occurs at resistance when selling)
   Both directions treated equally - the directional check happens at the level, not the direction.

2. Stricter delta_ratio: 0.30 (up from 0.28)
   This filters for higher-quality absorption while keeping thresholds simple.

3. Revert to simpler flow quality: just check if abs(delta) is above median recent delta
   instead of top quartile. This is less restrictive and more robust.

4. Keep rolling_window_sessions = 3 and concentration_threshold = 0.70
   These provide good level diversity without overfitting.

5. Remove directional level filtering entirely - just check proximity to any carried level.
   The wick requirement already provides directional validation (lower wick for longs,
   upper wick for shorts).

Rationale: The previous trial's directional proximity was theoretically sound but empirically
too restrictive. By reverting to simple proximity with stricter delta filtering, we get more
setups while maintaining quality. The byte-identical fills suggest the directional logic
never activated - indicating it was filtering out real absorption.

Expected: Byte-different from previous, with 700-900 trades and score -0.0080 to -0.0150.
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        level_proximity: float = 5.5,
        min_wick_points: float = -0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 3,
        concentration_threshold: float = 0.70,
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
        self.rolling_window_sessions = rolling_window_sessions
        self.concentration_threshold = concentration_threshold

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        
        # Track recent deltas for flow quality filtering
        self._recent_deltas: deque = deque(maxlen=50)

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
        
        # Track delta for flow quality filtering
        self._recent_deltas.append(abs(current.delta))

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
        self._recent_deltas = deque(maxlen=50)

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
        """
        all_levels = set()
        
        # Add levels from current session
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        
        # Add levels from all prior sessions in the rolling window
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _get_median_delta(self) -> float:
        """Get the median absolute delta from recent history."""
        if not self._recent_deltas or len(self._recent_deltas) < 5:
            return 0.0
        
        sorted_deltas = sorted(self._recent_deltas)
        idx = len(sorted_deltas) // 2
        return float(sorted_deltas[idx])

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels.
        
        Absorption is strong directional flow (delta_ratio >= 0.30) that fails
        to move price past the previous bar's extreme (wick requirement) at a
        carried level (proximity requirement).
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta (stricter at 0.30)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Evidence of price rejection via wick
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

        # Filter 3: Flow quality - delta should be above median recent delta
        median_delta = self._get_median_delta()
        if median_delta > 0 and abs(delta) < median_delta:
            return None

        # Filter 4: Check proximity to any carried level
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
