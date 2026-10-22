"""
Volume profile absorption strategy - refined wick requirement for synthetic differentiation.

Campaign best performer: #81 at -0.0096 with 622 trades.

The synthetic test shows BYTE-IDENTICAL trades (70) despite parameter changes,
indicating the current wick requirement (0.5 points) is too strict for synthetic
data where bars have smaller ranges.

This trial reduces the wick requirement from 0.5 to 0.25 points, which should:
1. Trigger on synthetic bar structures (proving the change takes effect)
2. Still filter for genuine absorption (0.25 point wick is real price rejection)
3. Maintain quality on real data by requiring directional delta + proximity + wick

The rest of the strategy maintains #81's proven parameters:
- absorption_delta_ratio: 0.28 (strict directional filtering)
- level_proximity: 2.6 (reasonable proximity to carried levels)
- target_multiple: 4.0 (proven risk/reward ratio)
- concentration_threshold: 0.62 (selective level extraction)
- rolling_window_sessions: 3 (multi-session level carrying)

Expected: More than 70 trades on synthetic (proving change works), and score
better than -0.0096 on real data with 600-700 trades.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 2.6,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.62,
        rolling_window_sessions: int = 3,
        min_level_volume_pct: float = 0.05,
        min_wick_points: float = 0.25,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.concentration_threshold = concentration_threshold
        self.rolling_window_sessions = rolling_window_sessions
        self.min_level_volume_pct = min_level_volume_pct
        self.min_wick_points = min_wick_points

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
        """Extract levels where volume is above significance thresholds."""
        if not profile:
            return []

        max_vol = max(profile.values()) if profile else 0
        if max_vol == 0:
            return []

        concentration_threshold_vol = max_vol * self.concentration_threshold
        min_volume = max_vol * self.min_level_volume_pct
        
        selected_levels = [
            level for level, vol in profile.items()
            if vol >= concentration_threshold_vol and vol >= min_volume
        ]

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from the rolling window of sessions."""
        all_levels = set()
        
        # Add levels from current session
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        
        # Add levels from all prior sessions in the rolling window
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with wick requirement for price rejection signature.
        
        Absorption requires:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Price near a carried level (within 2.6 points)
        3. Wick showing price rejection (>= 0.25 points on opposite side)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Core filter: strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check proximity to carried level
        all_levels = self._get_carried_levels()
        if not all_levels:
            return None

        min_distance = min(abs(current.close - level) for level in all_levels)
        if min_distance > self.level_proximity:
            return None

        # Calculate stop and target, with wick requirement
        if delta > 0:  # Long - require lower wick showing rejection
            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
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
        
        else:  # Short - require upper wick showing rejection
            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
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
