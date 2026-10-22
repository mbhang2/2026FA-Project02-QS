"""
Volume profile absorption strategy - refined selectivity targeting #81's -0.0096.

Campaign analysis shows the performance hierarchy:
- #81: -0.0096 with 622 trades (EXCEPTIONAL - 10x better than #76)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

The synthetic test produced BYTE-IDENTICAL 70 trades, confirming core logic.

Key insight from #81's dominance: It trades 43% fewer times than #76 but scores
10x better. This is not marginal improvement - it's a fundamental difference in
selectivity. The current parameters are still not strict enough.

Critical observation: The previous trial already filters heavily but #81 must be
filtering on something else OR the thresholds need to be even tighter. Looking at
the parameter space:

Current (previous trial):
- delta_ratio_long: 0.42, delta_ratio_short: 0.45
- min_abs_delta: 130
- level_proximity: 1.0
- max_stop_distance: 1.8
- target_multiple: 2.0 / 2.2

#81's likely approach (reverse-engineered from 622 trades vs 1079):
- Must have MUCH stricter delta_ratio (0.48+ for both directions)
- Must have MUCH higher min_abs_delta (150+)
- Must have MUCH tighter level_proximity (0.5 points)
- Must have MUCH tighter max_stop_distance (1.4)
- Must have DIFFERENT target multiples (tighter for one direction)
- Must skip more early-session bars (bars 5+ instead of 3+)

This trial increases selectivity across all dimensions to approach #81:
1. delta_ratio_long: 0.48, delta_ratio_short: 0.50 (extreme one-sidedness)
2. min_abs_delta: 150 (very high conviction)
3. level_proximity: 0.5 (must be nearly exactly at level)
4. max_stop_distance: 1.4 (very tight stops)
5. target_multiple_long: 1.8, target_multiple_short: 2.0 (conservative)
6. bars_into_session_before_trading: 5 (skip most early bars)
7. Only top 2 levels per session (ultra-sparse)

Expected: Score near #81's -0.0096 with 500-650 trades on real data.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        level_proximity: float = 0.5,
        min_wick_points: float = 0.65,
        stop_buffer: float = 0.25,
        target_multiple_long: float = 1.8,
        target_multiple_short: float = 2.0,
        max_stop_distance: float = 1.4,
        min_stop_distance: float = 0.25,
        size: int = 1,
        top_levels_per_session: int = 2,
        delta_ratio_long: float = 0.48,
        delta_ratio_short: float = 0.50,
        min_abs_delta: int = 150,
        bars_into_session_before_trading: int = 5,
    ) -> None:
        self.interval = interval
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple_long = target_multiple_long
        self.target_multiple_short = target_multiple_short
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.top_levels_per_session = top_levels_per_session
        self.delta_ratio_long = delta_ratio_long
        self.delta_ratio_short = delta_ratio_short
        self.min_abs_delta = min_abs_delta
        self.bars_into_session_before_trading = bars_into_session_before_trading

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        self._bars_in_current_session: int = 0
        
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
            self._bars_in_current_session = 0

        self._bars_in_current_session += 1

        # Accumulate volume profile for current session
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Skip early session bars (warm-up period)
        if self._bars_in_current_session <= self.bars_into_session_before_trading:
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
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

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
        top_levels = [level for level, vol in sorted_items[:self.top_levels_per_session]]

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
        Detect extreme high-quality absorption at carried volume levels.
        
        Ultra-strict parameters targeting #81's -0.0096 performance:
        - delta_ratio_long: 0.48 (extreme buying conviction)
        - delta_ratio_short: 0.50 (extreme selling conviction)
        - min_abs_delta: 150 (very high magnitude)
        - level_proximity: 0.5 (must be nearly exactly at level)
        - max_stop_distance: 1.4 (very tight stops)
        - min_wick_points: 0.65 (pronounced price rejection)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        
        # Require high absolute delta magnitude
        if abs(delta) < self.min_abs_delta:
            return None
        
        delta_ratio = abs(delta) / current.volume

        # Require pronounced wick showing strong price rejection
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

        # Directional delta ratio thresholds - extreme selectivity
        if delta > 0:  # Long
            if delta_ratio < self.delta_ratio_long:
                return None
        else:  # Short
            if delta_ratio < self.delta_ratio_short:
                return None

        # Check extremely tight proximity to carried level
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
                "target_distance": stop_distance * self.target_multiple_long,
            }
        
        else:  # Short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple_short,
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
