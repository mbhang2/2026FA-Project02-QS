"""
Volume profile absorption strategy - testing initiation filtering with realistic thresholds.

The synthetic test showed byte-identical trades to the current strategy, meaning
the initiation logic was not actually triggering. Root cause: the thresholds for
initiation confirmation were unrealistic for actual market bars.

Key insight from prior trials:
- #81: -0.0096 with 622 trades (BEST SCORE) - simple single-bar detection
- #76: -0.0893 with 1079 trades - moderate trade frequency
- #74: -0.0980 with 2763 trades - higher frequency

The campaign explicitly requires testing whether waiting for initiation helps.
Previous attempts had the logic but thresholds prevented execution.

This trial makes initiation actually work by:
1. Using #81's proven base parameters (delta_ratio=0.28, level_proximity=3.5)
2. Setting initiation_quality_threshold at 0.32 (higher than absorption's 0.28)
3. Requiring initiation bar to have body_ratio >= 0.45 (realistic, not 0.55)
4. Checking that initiation bar moves in same direction as absorption delta
5. Allowing initiation bar to be 1-2 bars after absorption (not immediate)
6. Tagging entries as "absorption-immediate" vs "absorption-initiation" for comparison

The synthetic test failure suggests the previous initiation check never qualified.
This version lowers thresholds to realistic levels so the initiation path actually
executes on real data, allowing the campaign question to be answered: does waiting
for initiation improve the outcome?

Expected: Some bars will now trigger initiation waiting instead of immediate entry.
Real data should show meaningful comparison between the two entry paths.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        initiation_quality_threshold: float = 0.32,
        level_proximity: float = 3.5,
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
        initiation_body_ratio: float = 0.45,
        initiation_patience: int = 2,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.initiation_quality_threshold = initiation_quality_threshold
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
        self.initiation_body_ratio = initiation_body_ratio
        self.initiation_patience = initiation_patience

        # Level tracking
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
        # Track bars for volume context
        self._session_volumes: list[int] = []
        
        # Pending absorption setup waiting for initiation
        self._pending_absorption: Optional[dict] = None
        self._pending_bars_waited: int = 0
        
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
            self._pending_absorption = None
            self._pending_bars_waited = 0

        # Accumulate volume profile and track volumes
        self._update_session_profile(current)
        self._session_volumes.append(current.volume)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_absorption = None
            self._pending_bars_waited = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Check if we're waiting for initiation confirmation
        if self._pending_absorption:
            self._pending_bars_waited += 1
            
            # Check if this bar shows initiation
            if self._check_initiation(current):
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, self._pending_absorption, tag_suffix="initiation")
                self._pending_absorption = None
                self._pending_bars_waited = 0
                return
            
            # Give up on pending if we've waited too long or price moved too far
            if self._pending_bars_waited > self.initiation_patience:
                self._pending_absorption = None
                self._pending_bars_waited = 0
            elif self._pending_absorption and abs(current.close - self._pending_absorption["level"]) > self.level_proximity * 2:
                self._pending_absorption = None
                self._pending_bars_waited = 0

        # Detect new absorption setup
        setup = self._detect_absorption(current)
        if setup:
            # High quality absorption: enter immediately
            if setup["delta_ratio"] >= self.initiation_quality_threshold:
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, setup, tag_suffix="immediate")
            else:
                # Lower quality: wait for initiation confirmation
                self._pending_absorption = setup
                self._pending_bars_waited = 0

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
        self._pending_bars_waited = 0

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
        """Extract levels where volume concentrated significantly."""
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

    def _check_initiation(self, current) -> bool:
        """
        Check if current bar shows initiation characteristics.
        
        Initiation bar should show:
        1. Sufficient body relative to range (>= initiation_body_ratio)
        2. Movement in same direction as the pending absorption delta
        """
        if not self._pending_absorption:
            return False

        bar_range = current.high - current.low
        if bar_range <= 0:
            return False

        # Check body ratio
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range
        if body_ratio < self.initiation_body_ratio:
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
        Detect absorption using #81's proven parameters.
        
        delta_ratio: 0.28 (proven by #81)
        min_wick_ratio: 0.40 (proven by #81)
        level_proximity: 3.5 (proven by #81)
        min_body_ratio: 0.50 (proven by #81)
        min_tick_count: 8 (proven by #81)
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

        # PRE-FILTER 3: Body ratio
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
