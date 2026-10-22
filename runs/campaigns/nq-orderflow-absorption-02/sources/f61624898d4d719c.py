"""
Volume profile absorption strategy - testing whether stricter wick requirements improve outcomes.

Campaign analysis shows:
- #81 (BEST): -0.0096 with 622 trades
- #146: -0.0611 with 907 trades
- Current synthetic: Byte-identical to #81

The byte-identical synthetic result indicates the core logic is sound. The question is
whether we can improve the real-data score by being more selective about which absorption
bars we trade.

Key insight: #81 used min_wick_points=0.0, meaning ANY wick was sufficient.
This is very permissive. An absorption bar with a tiny wick might not represent
genuine price rejection - it could just be noise.

This trial tests: stricter wick requirements filter out low-quality absorptions.

New approach:
1. Require wick to be a meaningful fraction of the bar's range (min_wick_ratio)
2. Require sufficient bar activity (min_tick_count)
3. Require body to be substantial relative to range (min_body_ratio)
4. Keep #81's proven core parameters: delta_ratio=0.28, level_proximity=3.5

These pre-filters will reduce trade count but should improve quality by trading only
bars that show genuine absorption (strong one-way delta + clear price rejection + activity).

The synthetic data being identical suggests these pre-filters either:
A) Don't eliminate any synthetic bars (they're all high-quality)
B) Eliminate bars but the remaining ones trade identically

Either way, on real data with more noise, stricter quality filters should help.

Expected: Fewer trades than #81 (maybe 400-500), but better score due to higher-conviction setups.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 3.5,
        min_wick_ratio: float = 0.35,
        min_body_ratio: float = 0.40,
        min_tick_count: int = 6,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_concentration_percentile: float = 0.70,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_ratio = min_wick_ratio
        self.min_body_ratio = min_body_ratio
        self.min_tick_count = min_tick_count
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_concentration_percentile = level_concentration_percentile

        # Level tracking with proper multi-session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
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

        # Accumulate volume profile for current session
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels - enter immediately
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and carry forward."""
        if self._session_levels:
            self._prior_session_levels.append(dict(self._session_levels))
            # Keep only last 2 sessions
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels with 0.25-point granularity."""
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

    def _extract_concentrated_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract levels where volume is in top percentile."""
        if not profile:
            return []

        volumes = sorted(profile.values(), reverse=True)
        if not volumes:
            return []

        # Find the volume threshold at the given percentile
        percentile_idx = max(0, int(len(volumes) * (1.0 - self.level_concentration_percentile)))
        threshold_vol = volumes[percentile_idx] if percentile_idx < len(volumes) else 0

        selected_levels = [level for level, vol in profile.items() if vol >= threshold_vol]
        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = set()
        
        # Current session levels
        all_levels.update(self._extract_concentrated_levels(self._session_levels))
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            all_levels.update(self._extract_concentrated_levels(session_profile))
        
        return sorted(list(all_levels))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with stricter quality filters.
        
        Core signature:
        - Strong directional delta (delta_ratio >= 0.28)
        - Meaningful wick in opposite direction (>= min_wick_ratio of range)
        - Substantial body (>= min_body_ratio of range)
        - Sufficient activity (tick_count >= min_tick_count)
        - Close near a carried level (within 3.5 points)
        
        These pre-filters ensure we only trade high-conviction absorption bars,
        not just any bar with a delta and a wick.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        bar_range = current.high - current.low
        
        # PRE-FILTER 1: Sufficient activity
        if current.tick_count < self.min_tick_count:
            return None

        # PRE-FILTER 2: Substantial body (directional commitment)
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range if bar_range > 0 else 0
        if body_ratio < self.min_body_ratio:
            return None

        # Core filter: delta_ratio must be strong
        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for meaningful wick in opposite direction to delta
        has_meaningful_wick = False
        if delta > 0:  # Buying pressure, check for lower wick (price rejection)
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0
            if wick_ratio >= self.min_wick_ratio:
                has_meaningful_wick = True
        elif delta < 0:  # Selling pressure, check for upper wick (price rejection)
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0
            if wick_ratio >= self.min_wick_ratio:
                has_meaningful_wick = True

        if not has_meaningful_wick:
            return None

        # Check proximity to carried level
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
