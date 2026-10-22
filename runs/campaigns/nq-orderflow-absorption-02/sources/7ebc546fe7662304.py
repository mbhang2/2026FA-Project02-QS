"""
Volume profile absorption strategy - targeting #81's -0.0096 with relaxed proximity.

Analysis of the byte-identical synthetic results: The previous trial's extremely
tight level_proximity (0.25) likely prevents trades even on synthetic data because
the condition is mathematically too strict.

Key insight from #81's success at -0.0096 with 622 trades:
- It must have a level proximity that actually permits trades
- The filtering comes from OTHER parameters, not proximity alone
- Stricter delta_ratio (0.45) and body_ratio (0.40) do the real work

This trial adjusts ONLY level_proximity back to a realistic range (1.5 points)
while keeping all other strict parameters that define quality:
- absorption_delta_ratio: 0.45 (extremely pure one-sided flow)
- level_proximity: 1.5 (realistic proximity to carried levels)
- min_body_ratio: 0.40 (very directional bars only)
- min_wick_points: 0.35 (clear price rejection)
- min_wick_ratio: 0.40 (wick is large fraction of range)
- min_tick_count: 10 (real compression, not noise)
- stop_buffer: 0.08 (tight structural placement)
- target_multiple: 1.5 (conservative targets)
- top_levels_per_session: 3 (only strongest levels)
- max_stop_distance: 1.0 (tight stops)
- min_stop_distance: 0.50 (meaningful risk)

The hypothesis: #81 succeeds through strict quality filtering on delta, body,
and wick, with a reasonable proximity threshold that actually permits trades.
Proximity of 1.5 points is tight enough to be meaningful but loose enough to
allow the high-quality setups through.

Expected: 500-700 trades with score better than -0.0600, targeting #81's -0.0096.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.45,
        level_proximity: float = 1.5,
        min_wick_points: float = 0.35,
        min_wick_ratio: float = 0.40,
        min_body_ratio: float = 0.40,
        stop_buffer: float = 0.08,
        target_multiple: float = 1.5,
        max_stop_distance: float = 1.0,
        min_stop_distance: float = 0.50,
        size: int = 1,
        min_tick_count: int = 10,
        top_levels_per_session: int = 3,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.min_wick_ratio = min_wick_ratio
        self.min_body_ratio = min_body_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_tick_count = min_tick_count
        self.top_levels_per_session = top_levels_per_session

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        
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
        """Extract only the strongest levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        top_levels = [level for level, _ in sorted_items[:self.top_levels_per_session]]
        
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

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption with strict filtering.
        
        Requirements:
        - Extremely strong one-sided delta (delta_ratio >= 0.45)
        - Meaningful activity (tick_count >= 10)
        - Clear price rejection (wick >= 0.35 AND ratio >= 0.40)
        - Very directional body (body_ratio >= 0.40)
        - Close to level (< 1.5 points)
        - Tight stop distance (0.50 to 1.0)
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

        # Extremely strict one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for very directional body
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range
        if body_ratio < self.min_body_ratio:
            return None

        # Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Long setup
        if delta > 0:
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range

            # Require BOTH absolute wick size AND ratio
            if lower_wick < self.min_wick_points or wick_ratio < self.min_wick_ratio:
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

        # Short setup
        else:
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range

            # Require BOTH absolute wick size AND ratio
            if upper_wick < self.min_wick_points or wick_ratio < self.min_wick_ratio:
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
