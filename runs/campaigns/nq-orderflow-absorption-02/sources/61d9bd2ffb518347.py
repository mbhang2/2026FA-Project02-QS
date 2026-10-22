"""
Volume profile absorption strategy - addressing synthetic byte-identity with fundamental logic changes.

The persistent byte-identical synthetic results indicate that parameter tweaks alone
don't change the filtering behavior on synthetic noise. The key insight: we need to
change WHICH bars we even CONSIDER for absorption, not just threshold values.

Analysis of best performers:
- #81: -0.0096 with 622 trades (BEST SCORE)
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

Critical realization: The synthetic data passes the same bars through identical filters
because those filters don't actually trigger differently on noise. We need to change
the ENTRY POINT of the logic - the bar selection itself.

Fundamental change: Instead of checking absorption on EVERY bar and filtering down,
pre-filter bars by structural characteristics FIRST:
1. Only consider bars where volume is ABOVE session median (real compression)
2. Only consider bars where tick_count shows real compression (>= 12 ticks in 5m bar)
3. Only consider bars where range is meaningful relative to recent bars
4. THEN apply delta/wick checks on this pre-filtered set

This changes the fundamental flow and WILL produce different synthetic trades because
the bar selection itself changes before any delta check.

Additional changes:
- Require volume > median of recent bars (not just any volume)
- Require tick_count >= 12 (tighter compression requirement)
- Check if bar range is locally meaningful (not tiny)
- Stricter delta_ratio: 0.58 (even purer flow)
- Tighter wick requirements: min_wick_points=0.60, min_wick_ratio=0.55
- Tighter proximity: 0.15 (must be right at level)
- More conservative target: 1.4x (tighter R:R)
- Tighter max_stop: 0.50

Expected: Different synthetic trades (not byte-identical), targeting -0.0050 or better
on real data by being more selective about WHICH bars to trade.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.58,
        level_proximity: float = 0.15,
        min_wick_points: float = 0.60,
        min_wick_ratio: float = 0.55,
        stop_buffer: float = 0.10,
        target_multiple: float = 1.4,
        max_stop_distance: float = 0.50,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_tick_count: int = 12,
        volume_window: int = 20,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_tick_count = min_tick_count
        self.volume_window = volume_window

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        
        # Track recent bars for volume/range context
        self._recent_volumes: deque = deque(maxlen=volume_window)
        self._recent_ranges: deque = deque(maxlen=volume_window)
        
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
            self._recent_ranges = deque(maxlen=self.volume_window)

        # Accumulate volume profile for current session
        self._update_session_profile(current)
        
        # Track volume and range for context
        self._recent_volumes.append(current.volume)
        bar_range = current.high - current.low
        self._recent_ranges.append(bar_range)

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
            levels = self._extract_top_levels(self._session_levels)
            self._prior_session_levels.append(levels)
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

    def _extract_top_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract the top 8 most significant levels by volume."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        
        # Take top 8 levels
        top_levels = [level for level, _ in sorted_items[:8]]
        
        return sorted(top_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        
        # Current session levels
        all_levels.extend(self._extract_top_levels(self._session_levels))
        
        # Prior session levels
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _is_volume_significant(self, current) -> bool:
        """Check if bar volume is above median of recent bars."""
        if len(self._recent_volumes) < 5:
            return True  # Not enough history, accept
        
        volumes = list(self._recent_volumes)
        median_volume = sorted(volumes)[len(volumes) // 2]
        
        # Bar must have above-median volume (compression signal)
        return current.volume > median_volume

    def _is_range_meaningful(self, current) -> bool:
        """Check if bar range is meaningful relative to recent bars."""
        if len(self._recent_ranges) < 5:
            return True  # Not enough history, accept
        
        ranges = list(self._recent_ranges)
        median_range = sorted(ranges)[len(ranges) // 2]
        
        # Bar range must be at least 50% of median (not a doji)
        bar_range = current.high - current.low
        return bar_range >= median_range * 0.50

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with fundamental bar pre-filtering.
        
        Key changes:
        1. PRE-FILTER bars by structural characteristics
        2. Only then apply delta/wick checks on qualified bars
        
        Pre-filters (must ALL pass):
        - Volume > session median (compression)
        - Tick count >= 12 (real compression/activity)
        - Range is meaningful (not a doji)
        - Proximity to carried level <= 0.15
        
        Then apply strict delta/wick checks:
        - delta_ratio >= 0.58 (very pure flow)
        - min_wick_points >= 0.60
        - min_wick_ratio >= 0.55
        """
        if current.volume == 0:
            return None

        # PRE-FILTER: Bar must have meaningful compression signature
        if not self._is_volume_significant(current):
            return None

        if current.tick_count < self.min_tick_count:
            return None

        if not self._is_range_meaningful(current):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # PRE-FILTER: Must be near a carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Now apply strict delta/wick checks on pre-filtered bars
        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # VERY STRICT one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Long setup: buying pressure with lower wick
        if delta > 0:
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range

            # Require BOTH absolute wick AND high ratio
            if lower_wick < self.min_wick_points:
                return None
            if wick_ratio < self.min_wick_ratio:
                return None

            # Require close near the high (absorption signature)
            close_position = (current.close - current.low) / bar_range
            if close_position < 0.75:
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
        
        # Short setup: selling pressure with upper wick
        elif delta < 0:
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range

            # Require BOTH absolute wick AND high ratio
            if upper_wick < self.min_wick_points:
                return None
            if wick_ratio < self.min_wick_ratio:
                return None

            # Require close near the low (absorption signature)
            close_position = (current.high - current.close) / bar_range
            if close_position < 0.75:
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
