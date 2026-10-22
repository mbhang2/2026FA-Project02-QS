"""
Volume profile absorption strategy - restore immediate entry with refined filters.

Campaign analysis reveals the core issue:
- #81: -0.0096 with 622 trades (BEST OVERALL)
- #176+: Initiation approach byte-identical on synthetic (70 trades)

The initiation logic produces byte-identical trades on synthetic because the
absorption detection itself is so restrictive that almost no absorptions are
found, so the initiation confirmation never matters. The problem is not the
initiation logic - it's that we're waiting for confirmation when we should be
entering on absorption itself.

Key insight: #81's success came from IMMEDIATE entry on absorption detection,
not from waiting for initiation. The initiation question in the campaign is
optional - "a rule that never waits is as useful as one that always does."

This trial returns to immediate entry but with REFINED absorption detection:
1. Keep multi-session level carrying (proven structure)
2. Keep #81's delta_ratio=0.28 (proven selectivity)
3. Tighten level_proximity to 4.0 (stricter proximity requirement)
4. Restore wick requirement: min_wick_points=-0.5 (proven filter)
5. Add volume concentration check (bar-level, not level-level)
6. Enter IMMEDIATELY on absorption (no initiation wait)
7. Tag all entries as "absorption" (no initiation distinction)

The refinement addresses the byte-identity problem by adding a NEW filter
(volume concentration) that will change which bars trigger entries on both
synthetic and real data. This is different from the initiation logic which
merely delays entry.

Volume concentration filter: A bar's volume should be concentrated in the
direction of its delta. For long absorption (buying pressure), volume should
cluster toward the low (close near low). For short, toward the high.

Expected: Different trade count on synthetic (proving new filter takes effect),
and score -0.0090 to -0.0100 with 600-700 trades by being more selective
about absorption bars while entering immediately.
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
        min_wick_points: float = -0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.65,
        rolling_window_sessions: int = 3,
        volume_concentration_ratio: float = 0.60,
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
        self.concentration_threshold = concentration_threshold
        self.rolling_window_sessions = rolling_window_sessions
        self.volume_concentration_ratio = volume_concentration_ratio

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        
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

        # Accumulate volume into current session profile
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels with volume concentration filter
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
        """Extract levels where volume is above significance threshold."""
        if not profile:
            return []

        max_vol = max(profile.values()) if profile else 0
        if max_vol == 0:
            return []

        threshold_vol = max_vol * self.concentration_threshold
        selected_levels = [level for level, vol in profile.items() if vol >= threshold_vol]

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

    def _is_volume_concentrated(self, bar) -> bool:
        """
        Check if the bar's volume is concentrated in one direction.
        
        For long absorption (delta > 0): volume concentrated toward low
        For short absorption (delta < 0): volume concentrated toward high
        
        Concentration is measured as how close the close is to the extreme
        in the direction of the delta.
        """
        if bar.volume <= 0 or bar.high <= bar.low:
            return False
        
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False
        
        # For buying pressure (delta > 0), close should be near the low
        if bar.delta > 0:
            distance_to_low = bar.close - bar.low
            concentration = distance_to_low / bar_range
            # Concentrated if close is in lower portion (concentration < threshold)
            return concentration < (1.0 - self.volume_concentration_ratio)
        
        # For selling pressure (delta < 0), close should be near the high
        elif bar.delta < 0:
            distance_to_high = bar.high - bar.close
            concentration = distance_to_high / bar_range
            # Concentrated if close is in upper portion (concentration < threshold)
            return concentration < (1.0 - self.volume_concentration_ratio)
        
        return False

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels with multiple filters.
        
        Absorption signature requires:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Evidence of price rejection via wick (min_wick_points >= -0.5)
        3. Volume concentrated in direction of delta
        4. Price within level_proximity of a carried level
        
        Returns setup dict for immediate entry, None if no absorption.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
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

        # Filter 3: Volume must be concentrated in direction of delta
        if not self._is_volume_concentrated(current):
            return None

        # Filter 4: Check proximity to carried level
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
