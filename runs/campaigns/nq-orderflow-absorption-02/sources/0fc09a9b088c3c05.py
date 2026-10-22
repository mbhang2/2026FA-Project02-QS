"""
Volume profile absorption strategy - test immediate entry with tighter filters.

The byte-identical synthetic results indicate that confirmation-based approaches
are not filtering differently on synthetic data. This suggests either:
1. The confirmation condition is too lenient and nearly always triggers
2. The synthetic data doesn't have the bar sequences needed to show the difference
3. The real difference lies elsewhere in the strategy

Analysis of best performers:
- #81: -0.0096 (622 trades) - immediate entry, delta_ratio 0.28, target 4.0
- #165: -0.0350 (658 trades) - immediate entry, delta_ratio 0.28, target 4.0
- #195: +0.0646 (535 trades) - only positive performer, tighter filters

Key insight: #195 was positive, meaning it LOST LESS than baseline. This suggests
that being MORE selective (fewer trades, tighter entry conditions) might be the path
forward rather than adding confirmation delays.

New approach: Return to immediate entry but make the absorption detection itself
MORE selective by:

1. Increase delta_ratio to 0.30 (require very strong directional flow)
2. Require close position relative to bar range to show price rejection
   - For long: close should be in lower 30% of range (price rejected upside)
   - For short: close should be in upper 30% of range (price rejected downside)
3. Increase min_wick to 0.75 (require significant wicks)
4. Reduce level_proximity to 3.0 (only trade at very close proximity to levels)
5. Increase target_multiple to 4.5 (better risk/reward)
6. Add a volume filter: require above-average volume for the bar
7. Reduce rolling window to 1 session (more recent levels only)

The hypothesis: The best trades are at carried levels with VERY strong absorption
signatures (high delta + significant wick + close in rejection zone + good volume).
These are rarer but higher quality.

Expected: 400-550 trades with higher win rate, targeting -0.0050 to -0.0100.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        level_proximity: float = 3.0,
        min_wick_points: float = 0.75,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 1,
        concentration_threshold: float = 0.75,
        close_position_threshold: float = 0.30,
        volume_lookback: int = 20,
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
        self.close_position_threshold = close_position_threshold
        self.volume_lookback = volume_lookback

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = -1
        self._current_session_profile: dict[float, int] = {}
        
        # Track recent volumes for filtering
        self._recent_volumes: list = []
        
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
        
        # Track recent volumes for filtering
        self._update_recent_volumes(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect high-quality absorption at carried levels
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
            if len(self._session_profiles) > self.rolling_window_sessions:
                self._session_profiles.pop(0)
        
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

    def _update_recent_volumes(self, bar) -> None:
        """Track recent volumes for filtering."""
        self._recent_volumes.append(bar.volume)
        if len(self._recent_volumes) > self.volume_lookback:
            self._recent_volumes.pop(0)

    def _get_average_volume(self) -> float:
        """Get average volume from recent bars."""
        if not self._recent_volumes:
            return 1.0
        return sum(self._recent_volumes) / len(self._recent_volumes)

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract levels representing the top concentration_threshold of volume."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        cumulative_vol = 0
        selected_levels = []
        
        for level, vol in sorted_items:
            selected_levels.append(level)
            cumulative_vol += vol
            if cumulative_vol >= total_vol * self.concentration_threshold:
                break

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
        Detect high-quality absorption at carried levels.
        
        Strict filters for high-quality setups:
        1. Delta ratio >= 0.30 (very strong directional flow)
        2. Significant wick >= 0.75 points (clear price rejection)
        3. Close positioned in rejection zone (lower 30% for long, upper 30% for short)
        4. Volume above average (confirms conviction)
        5. Proximity to carried level <= 3.0 points
        6. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Very strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Above-average volume
        avg_vol = self._get_average_volume()
        if current.volume < avg_vol:
            return None

        # Filter 3: Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with price rejection
        if delta > 0:
            # Require significant lower wick
            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None
            
            # Require close in lower 30% of range (price rejected upside)
            close_position = (current.close - current.low) / bar_range
            if close_position > self.close_position_threshold:
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
        
        # SHORT: Selling pressure with price rejection
        elif delta < 0:
            # Require significant upper wick
            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None
            
            # Require close in upper 30% of range (price rejected downside)
            close_position = (current.high - current.close) / bar_range
            if close_position > self.close_position_threshold:
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
        
        return None

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on high-quality absorption detection."""
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
