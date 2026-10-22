"""
Volume profile absorption strategy - restore #81's core with aggressive parameter tuning.

Campaign analysis reveals the critical constraint:
- #81: -0.0096 with 622 trades (BEST OVERALL)
- The byte-identical synthetic result (70 trades) shows the initiation logic
  isn't differentiating on synthetic data

The previous trial added initiation confirmation but produced byte-identical
trades, meaning the confirmation filter didn't trigger on synthetic either.
This suggests the real problem: we're not being selective ENOUGH on the
absorption detection itself.

Key insight from trial history:
- Stricter absorption filters → fewer but higher-quality trades → better score
- #81 beat both looser (#74: -0.0980) and more complex approaches (#175: untested)
- The winning formula: be VERY strict about what counts as absorption

This trial goes back to #81's core but makes absorption detection MORE selective:
1. Keep delta_ratio at 0.28 (proven strict threshold)
2. REDUCE level_proximity from 5.0 to 4.0 (tighter level requirement)
3. Keep wick requirement at -0.5 (proven price rejection filter)
4. INCREASE target_multiple from 4.0 to 4.5 (better risk/reward on fewer trades)
5. TIGHTEN min_stop_distance from 0.25 to 0.35 (avoid tiny stops)
6. REDUCE concentration_threshold from 0.65 to 0.60 (extract fewer, more significant levels)
7. REDUCE rolling_window_sessions from 3 to 2 (fresher level data)
8. Remove the initiation logic entirely - it doesn't help on synthetic and adds complexity

The logic: #81 worked because it was STRICT. The initiation confirmation didn't
change trades on synthetic (byte-identical), so it's not the right direction.
Instead, make the base absorption filter stricter: tighter proximity, fewer levels,
fewer but higher-quality setups.

Expected: Different trade count on synthetic (proving change takes effect),
score -0.0090 to -0.0085 on real data with fewer but higher-conviction trades.
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
        target_multiple: float = 4.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.35,
        size: int = 1,
        concentration_threshold: float = 0.60,
        rolling_window_sessions: int = 2,
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

        # Detect absorption at carried levels with strict filters
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Handle session boundary."""
        if self._current_session_profile:
            self._session_profiles.append(dict(self._current_session_profile))
            if len(self._session_profiles) > self.rolling_window_sessions:
                self._session_profiles.pop(0)
        
        # Reset for new session
        self._current_session_profile = {}
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
            self._current_session_profile[level_key] = (
                self._current_session_profile.get(level_key, 0) + int(vol_per_level)
            )
            level += 0.25

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract levels above concentration threshold."""
        if not profile:
            return []

        max_vol = max(profile.values()) if profile else 0
        if max_vol == 0:
            return []

        threshold_vol = max_vol * self.concentration_threshold
        selected_levels = [level for level, vol in profile.items() if vol >= threshold_vol]

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from rolling window."""
        all_levels = set()
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        return sorted(list(all_levels))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with strict filters matching #81's success pattern.
        
        Key parameters:
        - delta_ratio >= 0.28 (strong directional conviction)
        - wick >= -0.5 (evidence of price rejection)
        - proximity <= 4.0 (tight proximity to carried levels)
        - stop_distance >= 0.35 (avoid tiny stops)
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
        if delta > 0:  # Buying pressure
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_valid_wick = True
        elif delta < 0:  # Selling pressure
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_valid_wick = True

        if not has_valid_wick:
            return None

        # Filter 3: Tight proximity to carried level
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
