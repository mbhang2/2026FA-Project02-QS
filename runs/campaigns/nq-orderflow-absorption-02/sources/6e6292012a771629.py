"""
Volume profile absorption strategy - targeting #81's -0.0096 performance.

Campaign analysis shows:
- #81: -0.0096 with 622 trades (EXCEPTIONAL)
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

The synthetic test produces BYTE-IDENTICAL 70 trades, confirming core logic is sound.
The issue: parameters are reasonable but not aggressive enough to match #81's
performance. #81 trades 57% fewer times but scores 10x better.

Key insight: #81's superior performance comes from much stricter selectivity.
The current strategy filters are necessary but not sufficient. We need to:

1. Increase delta_ratio from 0.35 to 0.40 (very strong one-sided conviction)
2. Increase min_abs_delta from 90 to 120 (higher magnitude requirement)
3. Increase min_wick_points from 0.50 to 0.70 (very pronounced rejection)
4. Tighten level_proximity from 2.5 to 1.5 (much closer to actual levels)
5. Reduce target_multiple from 2.75 to 2.25 (tighter risk/reward, only best setups)
6. Reduce max_stop_distance from 2.5 to 2.0 (much tighter stops)
7. Reduce max_levels_per_session from 5 to 3 (only strongest levels)
8. Increase min_volume_percentile from 0.10 to 0.15 (bar must show real conviction)

The hypothesis: #81 achieves 10x better returns through extreme selectivity:
- Only the strongest one-sided flow (delta_ratio >= 0.40)
- Only bars with substantial volume concentration (>15% of session)
- Only bars with very pronounced price rejection (wick >= 0.70)
- Only at very tight proximity to the strongest volume nodes (1.5 points)
- Only with conservative targets (2.25x stop distance)
- Only the top 3 volume levels per session (ultra-sparse reference)

This dramatically reduces false signals while improving quality of remaining trades.

Expected: Score near #81's -0.0096 with 400-550 trades on real data.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.40,
        min_abs_delta: int = 120,
        level_proximity: float = 1.5,
        min_wick_points: float = 0.70,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.25,
        max_stop_distance: float = 2.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_levels_per_session: int = 3,
        min_volume_percentile: float = 0.15,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_abs_delta = min_abs_delta
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.max_levels_per_session = max_levels_per_session
        self.min_volume_percentile = min_volume_percentile

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._session_total_volume: int = 0
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
            levels = self._extract_levels_from_profile(self._session_levels)
            self._prior_session_levels.append(levels)
            # Keep only last 2 sessions
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._session_total_volume = 0
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        self._session_total_volume += bar.volume

        # Distribute volume across 0.25-point granularity
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _extract_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract only the strongest levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)

        # Keep only top N most significant levels (ultra-sparse)
        top_levels = [level for level, vol in sorted_items[:self.max_levels_per_session]]

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        
        # Current session levels
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        
        # Prior session levels
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect extremely high-quality absorption at carried volume levels.
        
        Ultra-strict parameters targeting #81-like performance:
        - delta_ratio: 0.40 (very strong one-sided flow)
        - min_abs_delta: 120 (high conviction magnitude)
        - level_proximity: 1.5 (very tight to actual levels)
        - min_wick_points: 0.70 (very pronounced rejection)
        - target_multiple: 2.25 (conservative targets)
        - min_volume_percentile: 0.15 (bar must show real conviction)
        - max_levels_per_session: 3 (only strongest nodes)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        
        # Require high absolute delta magnitude
        if abs(delta) < self.min_abs_delta:
            return None
        
        delta_ratio = abs(delta) / current.volume

        # Very strict one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Volume concentration check: bar must show real conviction
        if self._session_total_volume > 0:
            vol_pct = current.volume / self._session_total_volume
            if vol_pct < self.min_volume_percentile:
                return None

        # Require very pronounced wick showing strong price rejection
        has_wick = False
        if delta > 0:  # Buying pressure
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Check very tight proximity to carried level
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
