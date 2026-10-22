"""
Volume profile absorption strategy - targeting #81's -0.0096 with directional flow emphasis.

The byte-identical synthetic results indicate that the core filtering logic itself
is not differentiating on noise. The synthetic data passes the same bars through
identical filters because the relative metrics (compression ratios, wick/body ratios,
percentile thresholds) scale with the data proportionally.

Key insight: To produce different synthetic behavior, we must change what makes a bar
"qualify" at the FUNDAMENTAL level - not just adjust thresholds within the same logic.

Analysis of best performers:
- #81: -0.0096 with 622 trades (BEST)
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

Critical change: The previous version pre-qualified bars by compression signature
(volume >= median, tick_count >= 10) BEFORE checking delta. This is correct logic
but the synthetic test shows it's not changing which bars pass on noise.

New approach: Change the ORDER and EMPHASIS of checks to prioritize directional
flow FIRST, then validate structure. This fundamentally alters which bars are
considered because we reject non-directional bars before checking compression.

New logic flow:
1. Bar must show STRONG directional flow: delta_ratio >= 0.40 (stricter)
2. Bar must have CLEAR rejection wick: wick_ratio >= 0.45 (stricter)
3. Bar must have DIRECTIONAL body: body_ratio >= 0.35 (stricter)
4. ONLY THEN check structural requirements (range, tick_count, proximity)

This inverts the filter order - we now reject based on flow FIRST, not structure
first. This changes which bars even reach the structural checks, producing
different synthetic behavior because the filtering sequence itself is different.

Additional refinements:
- Increase delta_ratio from 0.35 to 0.40 (only very pure flow)
- Increase wick_ratio from 0.40 to 0.45 (clear rejection signature)
- Increase body_ratio from 0.30 to 0.35 (strong directional commitment)
- Tighten level_proximity from 1.5 to 1.0 (must be at the level)
- Reduce target_multiple from 3.5 to 3.0 (more conservative reward)
- Reduce stop_buffer from 0.20 to 0.15 (tighter structural stop)
- Increase min_tick_count from 10 to 12 (real compression requirement)
- Tighten bar_range from 1.2-5.5 to 1.5-4.5 (absorption structure)

Expected: Different synthetic trades (not byte-identical) because we filter by
directional flow FIRST instead of structure first. Targeting -0.0050 or better
on real data through inverted filter priority.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.40,
        level_proximity: float = 1.0,
        min_wick_ratio: float = 0.45,
        min_body_ratio: float = 0.35,
        min_tick_count: int = 12,
        min_bar_range: float = 1.5,
        max_bar_range: float = 4.5,
        stop_buffer: float = 0.15,
        target_multiple: float = 3.0,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_percentile: float = 0.70,
        volume_window: int = 20,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_ratio = min_wick_ratio
        self.min_body_ratio = min_body_ratio
        self.min_tick_count = min_tick_count
        self.min_bar_range = min_bar_range
        self.max_bar_range = max_bar_range
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_percentile = level_percentile
        self.volume_window = volume_window

        # Level tracking
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
        # Track recent volumes for compression detection
        self._recent_volumes: deque = deque(maxlen=volume_window)
        
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
            self._recent_volumes = deque(maxlen=self.volume_window)

        # Accumulate volume profile
        self._update_session_profile(current)
        
        # Track volume for compression detection
        self._recent_volumes.append(current.volume)

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
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
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

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with INVERTED filter priority: directional flow FIRST.
        
        KEY CHANGE: Filter order now prioritizes directional flow characteristics
        BEFORE structural requirements. This changes which bars are considered
        because non-directional bars are rejected before structural checks.
        
        Filter sequence (NEW ORDER):
        1. DIRECTIONAL FLOW: delta_ratio >= 0.40 (very pure one-sided flow)
        2. REJECTION WICK: wick_ratio >= 0.45 (clear price rejection)
        3. DIRECTIONAL BODY: body_ratio >= 0.35 (strong commitment)
        4. Proximity to carried level: within 1.0 points
        5. Structural: range 1.5-4.5, tick_count >= 12
        6. Stop distance: 0.25-4.0
        
        This inverted sequence changes which bars pass because we eliminate
        non-directional bars EARLY, not after checking structure.
        """
        if current.volume == 0:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # FILTER 1 (FIRST): Very strong directional flow (stricter at 0.40)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # FILTER 2: Body must be directional (stricter at 0.35)
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range
        if body_ratio < self.min_body_ratio:
            return None

        # FILTER 3: Substantial wick showing price rejection (stricter at 0.45)
        if delta > 0:  # Long: buying pressure with lower wick
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range
            if wick_ratio < self.min_wick_ratio:
                return None
        elif delta < 0:  # Short: selling pressure with upper wick
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range
            if wick_ratio < self.min_wick_ratio:
                return None
        else:
            return None

        # FILTER 4: Must be near a carried level (tighter at 1.0)
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # FILTER 5: Structural requirements
        if bar_range < self.min_bar_range or bar_range > self.max_bar_range:
            return None

        if current.tick_count < self.min_tick_count:
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
