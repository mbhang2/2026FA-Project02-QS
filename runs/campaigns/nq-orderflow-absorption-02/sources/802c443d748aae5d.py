"""
Volume profile absorption strategy - testing lower delta_ratio threshold.

The synthetic test showed byte-identical trades to the current strategy (#81),
which uses delta_ratio=0.28. This means either:

1. The synthetic data has very few bars with delta_ratio >= 0.28, OR
2. The other filters are eliminating bars before delta_ratio is checked

Analysis of performance:
- #81: -0.0096 with 622 trades (BEST SCORE)
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

The pattern shows that LOWER trade frequency correlates with BETTER scores.
#81 with 622 trades beats #76 with 1079 trades. This suggests we're being
too permissive with absorption detection.

Key insight: If we lower delta_ratio from 0.28 to 0.22, we filter MORE strictly
for one-sided flow, which should:
1. Reduce total trades (fewer bars pass the filter)
2. Increase selectivity (only highest conviction absorption)
3. Match the pattern of #81 being better than #76

The synthetic test showed byte-identical trades, which suggests the current
parameters aren't differentiating on synthetic data. Lowering delta_ratio
creates a measurable change that SHOULD show different trades if the
parameter matters.

Additionally, I'm tightening level_proximity from 3.5 to 2.5 to require
closer proximity to carried levels. This adds another selectivity layer.

Expected: Fewer trades than #81 (~400-500), targeting score better than -0.0096
by focusing on only the highest-conviction absorption setups.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.22,
        level_proximity: float = 2.5,
        min_body_ratio: float = 0.50,
        min_wick_ratio: float = 0.40,
        min_tick_count: int = 8,
        min_bar_range: float = 1.5,
        max_bar_range: float = 8.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.60,
        volume_ratio_threshold: float = 0.80,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_body_ratio = min_body_ratio
        self.min_wick_ratio = min_wick_ratio
        self.min_tick_count = min_tick_count
        self.min_bar_range = min_bar_range
        self.max_bar_range = max_bar_range
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.concentration_threshold = concentration_threshold
        self.volume_ratio_threshold = volume_ratio_threshold

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
        # Track bars for volume context
        self._session_volumes: list[int] = []
        
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
            self._session_volumes = []

        # Accumulate volume profile for current session
        self._update_session_profile(current)
        self._session_volumes.append(current.volume)

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

    def _extract_concentration_levels(self, profile: dict[float, int]) -> list[float]:
        """
        Extract levels where volume concentrated significantly.
        
        Keep only levels where volume >= threshold * max_volume_at_any_level.
        """
        if not profile:
            return []

        max_vol = max(profile.values()) if profile else 0
        if max_vol == 0:
            return []

        threshold_vol = max_vol * self.concentration_threshold
        selected_levels = [level for level, vol in profile.items() if vol >= threshold_vol]

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels: dict[float, int] = {}
        
        # Current session levels
        for level in self._extract_concentration_levels(self._session_levels):
            all_levels[level] = all_levels.get(level, 0) + 1
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            for level in self._extract_concentration_levels(session_profile):
                all_levels[level] = all_levels.get(level, 0) + 1
        
        return sorted(list(all_levels.keys()))

    def _get_session_median_volume(self) -> float:
        """Get median volume of bars seen so far in this session."""
        if not self._session_volumes:
            return 0.0
        sorted_vols = sorted(self._session_volumes)
        return float(sorted_vols[len(sorted_vols) // 2])

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with stricter delta_ratio filter.
        
        Lowering delta_ratio from 0.28 to 0.22 requires even more one-sided flow,
        filtering for only the highest conviction absorption setups.
        
        Also tightening level_proximity from 3.5 to 2.5 for closer level proximity.
        """
        if current.volume == 0:
            return None

        bar_range = current.high - current.low

        # PRE-FILTER 1: Bar range bounds
        if bar_range < self.min_bar_range or bar_range > self.max_bar_range:
            return None

        # PRE-FILTER 2: Tick count
        if current.tick_count < self.min_tick_count:
            return None

        # PRE-FILTER 3: Body ratio (directional commitment)
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range if bar_range > 0 else 0
        if body_ratio < self.min_body_ratio:
            return None

        # PRE-FILTER 4: Volume above session median
        session_median = self._get_session_median_volume()
        if session_median > 0 and current.volume < session_median * self.volume_ratio_threshold:
            return None

        # Apply delta_ratio check - STRICTER threshold
        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check wick in OPPOSITE direction to delta (absorption signature)
        # LONG (delta > 0): require LOWER wick showing downward rejection
        if delta > 0:
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0
            
            if wick_ratio < self.min_wick_ratio:
                return None
        
        # SHORT (delta < 0): require UPPER wick showing upward rejection
        elif delta < 0:
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0
            
            if wick_ratio < self.min_wick_ratio:
                return None
        
        else:
            return None

        # Check proximity to carried level - TIGHTER proximity
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
