"""
Volume profile absorption strategy - fixing initiation logic to actually trigger.

The previous trial added initiation waiting logic but the synthetic test showed
byte-identical trades, meaning the initiation confirmation never actually fired.

Root cause analysis:
1. Pending absorption was stored but the next bar check had issues
2. The efficiency calculation was too complex and likely always failed
3. The initiation body_ratio (0.55) was too strict for real bars
4. The logic flow had the initiation check but it never qualified

Key insight: The synthetic test shows we're still taking immediate entries only.
This means either:
- Pending absorption is being set but initiation checks always fail
- Or the high_quality_delta_ratio (0.35) threshold is catching everything first

This trial simplifies initiation to actually work:
1. Lower initiation body_ratio from 0.55 to 0.45 (realistic threshold)
2. Simplify efficiency check: just require bar moved in same direction as delta
3. Lower high_quality_delta_ratio from 0.35 to 0.32 so some bars wait
4. Add explicit logging via tag to distinguish paths
5. Make initiation check actually reachable by adjusting thresholds

The campaign explicitly requires testing whether waiting for initiation helps.
The previous version had the logic but thresholds prevented it from triggering.
This version makes the initiation path actually executable on real data.

Expected: Different synthetic trades where some bars trigger initiation waiting
instead of immediate entry. Real data should show the comparison between
"absorption-immediate" vs "absorption-initiation" tags.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.25,
        high_quality_delta_ratio: float = 0.32,
        level_proximity: float = 6.0,
        min_body_ratio: float = 0.40,
        min_wick_ratio: float = 0.35,
        min_tick_count: int = 6,
        min_bar_range: float = 1.5,
        max_bar_range: float = 8.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_percentile: float = 0.70,
        volume_ratio_threshold: float = 0.80,
        min_initiation_body_ratio: float = 0.45,
        initiation_lookback: int = 5,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.high_quality_delta_ratio = high_quality_delta_ratio
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
        self.level_percentile = level_percentile
        self.volume_ratio_threshold = volume_ratio_threshold
        self.min_initiation_body_ratio = min_initiation_body_ratio
        self.initiation_lookback = initiation_lookback

        # Level tracking
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
        # Track bars for volume context
        self._session_volumes: list[int] = []
        
        # Track recent bars for range calculation
        self._recent_bars: deque = deque(maxlen=initiation_lookback)
        
        # Pending absorption setups waiting for initiation
        self._pending_absorption: Optional[dict] = None
        
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
            self._recent_bars = deque(maxlen=self.initiation_lookback)
            self._pending_absorption = None

        # Accumulate volume profile and track volumes
        self._update_session_profile(current)
        self._session_volumes.append(current.volume)
        self._recent_bars.append(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_absorption = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Check if we have a pending absorption setup waiting for initiation
        if self._pending_absorption:
            # Check if this bar shows initiation characteristics
            if self._check_initiation(current):
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, self._pending_absorption, tag_suffix="initiation")
                self._pending_absorption = None
                return
            else:
                # Check if absorption is stale (price moved too far)
                absorption_level = self._pending_absorption["level"]
                if abs(current.close - absorption_level) > self.level_proximity * 2:
                    self._pending_absorption = None

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            # Check if this is high quality (immediate entry)
            if setup["delta_ratio"] >= self.high_quality_delta_ratio:
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, setup, tag_suffix="immediate")
            else:
                # Store as pending, wait for initiation
                self._pending_absorption = setup

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and carry forward."""
        if self._session_levels:
            self._prior_session_levels.append(dict(self._session_levels))
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        self._session_levels = {}
        self._entered_bar_timestamps = set()
        self._pending_absorption = None

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

    def _extract_levels_by_percentile(self, profile: dict[float, int]) -> list[float]:
        """Extract levels representing top volume_percentile of total volume."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        cumulative_vol = 0
        threshold_vol = total_vol * self.level_percentile
        selected_levels = []

        for level, vol in sorted_items:
            selected_levels.append(level)
            cumulative_vol += vol
            if cumulative_vol >= threshold_vol:
                break

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels: dict[float, int] = {}
        
        # Current session levels
        for level in self._extract_levels_by_percentile(self._session_levels):
            all_levels[level] = all_levels.get(level, 0) + 1
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            for level in self._extract_levels_by_percentile(session_profile):
                all_levels[level] = all_levels.get(level, 0) + 1
        
        return sorted(list(all_levels.keys()))

    def _get_session_median_volume(self) -> float:
        """Get median volume of bars seen so far in this session."""
        if not self._session_volumes:
            return 0.0
        sorted_vols = sorted(self._session_volumes)
        return float(sorted_vols[len(sorted_vols) // 2])

    def _get_recent_avg_range(self) -> float:
        """Get average range of recent bars."""
        if len(self._recent_bars) < 3:
            return 1.0
        ranges = [b.high - b.low for b in list(self._recent_bars)[-3:]]
        return sum(ranges) / len(ranges) if ranges else 1.0

    def _check_initiation(self, current) -> bool:
        """
        Check if current bar shows initiation characteristics after absorption.
        
        Initiation bar should show:
        1. Large body relative to range (>= min_initiation_body_ratio)
        2. Range larger than recent average (shows expansion/momentum)
        3. Movement in same direction as the pending absorption delta
        """
        if not self._pending_absorption:
            return False

        bar_range = current.high - current.low
        if bar_range <= 0:
            return False

        # Check body ratio
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range
        if body_ratio < self.min_initiation_body_ratio:
            return False

        # Check that bar range is larger than recent average
        recent_avg = self._get_recent_avg_range()
        if bar_range < recent_avg * 0.8:
            return False

        # Check that movement is in the direction of the pending absorption
        pending_direction = self._pending_absorption["direction"]
        if pending_direction == "long":
            # For long, we want close > open (upward momentum)
            if current.close <= current.open:
                return False
        else:
            # For short, we want close < open (downward momentum)
            if current.close >= current.open:
                return False

        return True

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with corrected wick detection.
        
        The wick must be in the OPPOSITE direction of the delta (absorption signal):
        - LONG (delta > 0, buying pressure): require LOWER wick (rejection downward)
        - SHORT (delta < 0, selling pressure): require UPPER wick (rejection upward)
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

        # Apply delta_ratio check
        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check wick in OPPOSITE direction to delta (absorption signature)
        if delta > 0:
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0
            
            if wick_ratio < self.min_wick_ratio:
                return None
        
        elif delta < 0:
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0
            
            if wick_ratio < self.min_wick_ratio:
                return None
        
        else:
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
                "delta_ratio": delta_ratio,
                "level": current.close,
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
                "delta_ratio": delta_ratio,
                "level": current.close,
            }

    def _place_entry(self, ctx, setup: dict, tag_suffix: str = "absorption") -> None:
        """Place entry order on absorption detection."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]
        tag = f"absorption-{tag_suffix}"

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
