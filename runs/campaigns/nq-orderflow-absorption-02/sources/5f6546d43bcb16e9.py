"""
Volume profile absorption strategy - refined toward #81's -0.0096 with stricter filtering.

Campaign analysis: #81 achieved -0.0096 with 622 trades. Recent byte-identical
synthetic results indicate parameter adjustments were too incremental.

Key change: Make absorption detection meaningfully stricter to produce different
trade selection on real data while maintaining the structural logic that works.

Refined parameters:
- absorption_delta_ratio: 0.40 (very strong one-sided flow, stricter than #81's implicit)
- level_proximity: 0.75 (tighter proximity to actual levels)
- top_levels_per_session: 6 (fewer, only strongest levels)
- min_wick_points: 0.30 (clear price rejection requirement)
- min_body_ratio: 0.25 (meaningful bar body)
- target_multiple: 2.0 (more conservative, better risk/reward)
- max_stop_distance: 1.5 (tighter stops)
- min_tick_count: 5 (moderate activity requirement)
- stop_buffer: 0.10 (tight structural placement)

This produces meaningfully different filtering while preserving the core absorption
detection that made #81 successful. Expected: 400-600 trades with score better than
-0.0600, targeting #81's -0.0096.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.40,
        level_proximity: float = 0.75,
        min_wick_points: float = 0.30,
        min_body_ratio: float = 0.25,
        stop_buffer: float = 0.10,
        target_multiple: float = 2.0,
        max_stop_distance: float = 1.5,
        min_stop_distance: float = 0.35,
        size: int = 1,
        min_tick_count: int = 5,
        top_levels_per_session: int = 6,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.min_body_ratio = min_body_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_tick_count = min_tick_count
        self.top_levels_per_session = top_levels_per_session

        # Level tracking with persistence
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
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

    def _extract_strong_levels(self) -> list[float]:
        """
        Extract only the strongest levels across all sessions.
        Prioritize levels with highest accumulated volume.
        """
        # Aggregate volume across all sessions
        all_levels: dict[float, int] = {}
        
        # Current session
        for level, vol in self._session_levels.items():
            all_levels[level] = all_levels.get(level, 0) + vol
        
        # Prior sessions
        for session_profile in self._prior_session_levels:
            for level, vol in session_profile.items():
                all_levels[level] = all_levels.get(level, 0) + vol
        
        # Sort by total volume and take top levels only
        sorted_levels = sorted(all_levels.items(), key=lambda x: x[1], reverse=True)
        top_levels = [level for level, _ in sorted_levels[:self.top_levels_per_session]]
        
        return sorted(top_levels)

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption with strict filtering.
        
        Requirements:
        - Strong one-sided delta (delta_ratio >= 0.40)
        - Meaningful activity (tick_count >= 5)
        - Clear price rejection (wick >= 0.30)
        - Meaningful body (body_ratio >= 0.25)
        - Very close to strong level (< 0.75 points)
        - Reasonable stop distance (0.35 to 1.5)
        """
        if current.volume == 0:
            return None

        # Require meaningful activity
        if current.tick_count < self.min_tick_count:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Very strict one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for meaningful body
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range
        if body_ratio < self.min_body_ratio:
            return None

        # Require clear wick showing price rejection
        has_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to strong carried level
        carried_levels = self._extract_strong_levels()
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
