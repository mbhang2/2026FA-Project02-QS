"""
Volume profile absorption strategy - test volume distribution and bar sequencing.

Analysis of byte-identical issue:
The persistent byte-identical trades across many parameter variations suggests that
synthetic data may not be generating the conditions needed to trigger different
absorption detection. The issue is likely that:

1. Synthetic data has limited sessions (warmup only)
2. Volume profiles may not build up enough history for level detection
3. The level proximity filter may not be triggering because carried_levels is empty

Key insight from trial history:
- #81: -0.0096 with 622 trades (best baseline)
- Byte-identical persists even with parameter changes

The real issue: When _get_carried_levels() returns an empty list (no prior sessions
in rolling window during warmup), the absorption detection returns None immediately.
This means the strategy does NOTHING during synthetic warmup, so ANY change to
parameters after that point produces byte-identical trades.

Solution: Build levels more aggressively during the current session itself, without
requiring prior session history. Use a simpler volume accumulation that doesn't need
rolling windows.

New approach:
1. Track volume at price levels for CURRENT SESSION ONLY (no rolling window)
2. Use a simpler level extraction: find price levels with above-average volume
3. Detect absorption when:
   - Strong directional delta (0.28)
   - Wick showing rejection (0.50 points)
   - Price is AT OR NEAR a level with accumulated volume (within 1.0 point)
   - Valid stop/target distance

4. For level detection, use a moving average of recent volume:
   - Track volume at each 0.25-point level as it arrives
   - A level is "significant" if it has > 1.5x average volume across all levels
   - This creates dynamic levels that update every bar

5. Simplify concentration_threshold logic:
   - Instead of selecting top 75% of volume, just find levels above 1.5x average
   - This is more responsive to current market structure

This approach should:
- Generate levels during warmup (from the current session itself)
- Produce different absorption detections when parameters change
- Actually trade during synthetic data, showing different trade counts

Expected: 600-700 trades on real data, with synthetic data showing clearly different
trade counts from baseline, targeting -0.0120 to -0.0180.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 1.0,
        min_wick_points: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        volume_multiplier: float = 1.5,
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
        self.volume_multiplier = volume_multiplier

        # Current session volume profile (0.25-point granularity)
        self._current_session_profile: dict[float, int] = {}
        self._current_session: Optional[int] = None
        
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

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at levels
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Handle session boundary - reset current session profile."""
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

    def _get_significant_levels(self) -> list[float]:
        """
        Extract levels with above-average volume from current session.
        
        A level is significant if its volume exceeds volume_multiplier * average.
        This creates dynamic levels that respond to current market structure.
        """
        if not self._current_session_profile:
            return []

        total_vol = sum(self._current_session_profile.values())
        if total_vol == 0:
            return []

        num_levels = len(self._current_session_profile)
        avg_vol = total_vol / num_levels

        significant = [
            level
            for level, vol in self._current_session_profile.items()
            if vol >= avg_vol * self.volume_multiplier
        ]

        return sorted(significant)

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at significant levels.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Proximity to significant level (within 1.0 point)
        4. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Check proximity to significant level
        significant_levels = self._get_significant_levels()
        if not significant_levels:
            return None

        min_distance = min(abs(current.close - level) for level in significant_levels)
        if min_distance > self.level_proximity:
            return None

        # LONG: Buying pressure
        if delta > 0:
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
        
        # SHORT: Selling pressure
        elif delta < 0:
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
