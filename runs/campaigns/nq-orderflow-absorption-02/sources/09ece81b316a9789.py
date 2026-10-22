"""
Volume profile absorption strategy - restoring immediate entry path alongside initiation.

The synthetic test showed byte-identical trades because the previous strategy
ALWAYS waited for initiation, which on synthetic data either never triggered or
triggered identically to #81's immediate entry.

The campaign explicitly requires comparing immediate entry vs initiation waiting.
The previous trial only tested waiting, making the comparison impossible.

This trial restores BOTH paths:
1. Absorption bars with high delta_ratio (>= 0.32) enter IMMEDIATELY
2. Absorption bars with lower delta_ratio (0.28-0.32) wait for INITIATION
3. Both are tagged separately so the comparison is readable in the blotter

This ensures:
- The immediate entry path executes (visible in synthetic test)
- The initiation waiting path executes (when conditions allow)
- The campaign question is answerable: does waiting help?
- Trade count and timing will differ from previous trials

Key parameters from #81's success (-0.0096, 622 trades):
- delta_ratio: 0.28 (threshold for absorption detection)
- level_proximity: 3.5 (tight proximity to carried levels)
- min_wick_ratio: 0.40 (price rejection signature)
- min_body_ratio: 0.50 (directional commitment)
- min_tick_count: 8 (quality bars only)

Split logic:
- delta_ratio >= 0.32: high quality, enter immediately with tag "absorption-immediate"
- delta_ratio 0.28-0.32: lower quality, wait for initiation with tag "absorption-initiation"
- initiation_body_ratio: 0.45 (realistic threshold for confirmation)
- initiation_patience: 3 bars (reasonable wait window)

Expected: Better score than #81 by identifying which entry method works better.
Synthetic test should show different trade timing vs previous trials.
"""

from __future__ import annotations

from typing import Optional


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
        initiation_body_ratio: float = 0.45,
        initiation_patience: int = 3,
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
        self.initiation_body_ratio = initiation_body_ratio
        self.initiation_patience = initiation_patience

        # Level tracking
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
        # Pending absorption waiting for initiation
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

        # Accumulate volume profile for current session
        self._update_session_profile(current)

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
            
            # Give up on pending if we've waited too long
            if self._pending_bars_waited > self.initiation_patience:
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
        """Extract levels where volume is significantly concentrated."""
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
        all_levels = set()
        
        # Current session levels
        all_levels.update(self._extract_concentration_levels(self._session_levels))
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            all_levels.update(self._extract_concentration_levels(session_profile))
        
        return sorted(list(all_levels))

    def _check_initiation(self, current) -> bool:
        """
        Check if current bar shows initiation characteristics.
        
        Initiation bar must show:
        1. Sufficient body relative to range (>= initiation_body_ratio)
        2. Movement in same direction as the pending absorption
        3. Price still near the absorption level
        """
        if not self._pending_absorption:
            return False

        bar_range = current.high - current.low
        if bar_range <= 0:
            return False

        # Check body ratio - must show meaningful directional movement
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range
        if body_ratio < self.initiation_body_ratio:
            return False

        # Check that movement is in the direction of the pending absorption
        pending_direction = self._pending_absorption["direction"]
        if pending_direction == "long":
            # For long, need close > open (upward momentum)
            if current.close <= current.open:
                return False
        else:
            # For short, need close < open (downward momentum)
            if current.close >= current.open:
                return False

        # Verify price is still near the absorption level
        absorption_level = self._pending_absorption["level"]
        if abs(current.close - absorption_level) > self.level_proximity * 2:
            return False

        return True

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption using #81's proven parameters.
        
        Key filters (all from #81's -0.0096 score):
        - delta_ratio: 0.28 (strict filtering)
        - level_proximity: 3.5 (tight proximity)
        - min_wick_ratio: 0.40 (price rejection)
        - min_body_ratio: 0.50 (directional)
        - min_tick_count: 8 (quality bars)
        """
        if current.volume == 0:
            return None

        bar_range = current.high - current.low

        # PRE-FILTER 1: Bar range bounds
        if bar_range < self.min_bar_range or bar_range > self.max_bar_range:
            return None

        # PRE-FILTER 2: Tick count minimum
        if current.tick_count < self.min_tick_count:
            return None

        # PRE-FILTER 3: Body ratio (directional commitment)
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range if bar_range > 0 else 0
        if body_ratio < self.min_body_ratio:
            return None

        # Check delta and delta_ratio
        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check wick in opposite direction to delta (absorption signature)
        if delta > 0:  # Buying pressure, check lower wick
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0
            
            if wick_ratio < self.min_wick_ratio:
                return None
        
        elif delta < 0:  # Selling pressure, check upper wick
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
                "level": current.close,
                "delta_ratio": delta_ratio,
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
                "level": current.close,
                "delta_ratio": delta_ratio,
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
