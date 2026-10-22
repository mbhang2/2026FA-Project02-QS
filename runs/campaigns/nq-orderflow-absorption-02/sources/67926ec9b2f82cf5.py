"""
Volume profile absorption strategy - refined toward #81's -0.0096 performance.

The synthetic test produced IDENTICAL trades (70) to the current strategy,
confirming core logic is sound. The issue: we're producing the same number of
trades as before despite adding filters, which means the filters aren't actually
reducing trade count on the synthetic data.

Campaign analysis of best performers:
- #81: -0.0096 with 622 trades (EXCEPTIONAL: 10x better than #76)
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

Key insight: #81 trades 57% fewer times than #76 but scores 9x better. This
suggests #81 has much stricter entry criteria, not just parameter tweaks.

The synthetic test producing identical 70 trades means:
1. The volume_percentile filters aren't triggering (all 70 bars pass)
2. The carried_levels check isn't filtering (either no levels exist, or all
   bars happen to be near them)
3. We need MORE aggressive filtering that works even in early sessions

Critical realization: The current approach tries to be smart about early
sessions with different thresholds. Instead, we should be MUCH stricter
across the board. #81's 622 trades vs our ~70 on synthetic suggests it's
using fundamentally different criteria.

Revised strategy - go significantly stricter:
1. Increase delta_ratio to 0.38 (very strong one-sided flow)
2. Increase min_abs_delta to 100 (genuine volume conviction required)
3. Increase min_wick_points to 0.60 (very strong price rejection)
4. Decrease target_multiple to 2.0 (tighter risk/reward, only best setups)
5. Decrease max_stop_distance to 2.5 (much tighter stops)
6. Require at least 1 carried level to trade (not optional)
7. Tighten level_proximity to 2.0 (very close to actual levels)
8. Remove volume_percentile filters - they're not working
9. Add a bar body requirement: body must be >50% of range

The hypothesis: #81 is MUCH more selective. By requiring carried levels,
stricter delta/wick, and tighter proximity, we dramatically reduce false
signals while improving quality. This should produce 200-400 trades on real
data (vs 70 on synthetic), closer to #81's 622 with better selectivity.

Expected: Score near #81's -0.0096 with 400-550 trades on real data.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.38,
        min_abs_delta: int = 100,
        level_proximity: float = 2.0,
        min_wick_points: float = 0.60,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.0,
        max_stop_distance: float = 2.5,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_levels_per_session: int = 5,
        min_body_ratio: float = 0.50,
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
        self.min_body_ratio = min_body_ratio

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
        """Extract the most significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)

        # Keep only top N most significant levels
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
        Detect very high-quality absorption with strict selectivity.
        
        Key filters (much stricter than before):
        - Absolute delta minimum: 100 contracts (genuine conviction)
        - Delta ratio: 0.38 (very strong one-sided flow)
        - Level proximity: 2.0 (very tight to actual levels)
        - Min wick: 0.60 (very strong price rejection)
        - Require carried levels: must reference actual prior structure
        - Body ratio: body must be >50% of range (efficient move)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        
        # Require substantial absolute delta magnitude
        if abs(delta) < self.min_abs_delta:
            return None
        
        delta_ratio = abs(delta) / current.volume

        # Very strict ratio threshold
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require very clear wick showing price rejection
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

        # Check body ratio: body must be significant fraction of range
        bar_range = current.high - current.low
        if bar_range <= 0:
            return None
        
        if delta > 0:  # Long: body is close to high
            body = current.close - current.open
            if bar_range > 0:
                body_ratio = body / bar_range
                if body_ratio < self.min_body_ratio:
                    return None
        else:  # Short: body is open to close
            body = current.open - current.close
            if bar_range > 0:
                body_ratio = body / bar_range
                if body_ratio < self.min_body_ratio:
                    return None

        # REQUIRE carried levels - don't trade without reference points
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        # Check tight proximity to carried level
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
