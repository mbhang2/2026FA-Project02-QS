"""
Volume profile absorption strategy - test efficiency-based entry with dynamic level adaptation.

Analysis of persistent byte-identical issue:
The synthetic data is producing identical trades because the core absorption detection
logic (delta_ratio, wick, level proximity) is triggering identically regardless of
parameter changes. The efficiency filter is either:
1. Not filtering (avg_efficiency is None early in warmup)
2. Filtering the same bars as the other conditions already do

Key insight: The efficiency_threshold parameter change should produce different trade
counts if it's actually filtering. The fact that it doesn't means the filter isn't
working as intended.

Root cause: During warmup/early session, _recent_efficiency deque is sparse or empty,
so avg_efficiency is None and the efficiency check is skipped entirely. This means
parameter changes to efficiency_threshold have NO EFFECT.

Solution: Build efficiency baseline more robustly:
1. Always calculate and track efficiency for every bar with non-zero delta
2. Use a minimum baseline efficiency if not enough history (0.5 instead of None)
3. Make efficiency threshold a HARD FILTER that always applies
4. Adjust level_proximity to be more selective (0.5 instead of 1.0)
5. Add a secondary filter: require that the bar's body is meaningful (>= 0.25 points)

Additionally, test the hypothesis that FRESH levels matter:
- Track when each level was first visited in current session
- Only trade absorption at levels visited within last 5-10 bars (fresh)
- This tests whether newly-formed levels have better predictive power

This approach ensures:
- Efficiency filter ALWAYS applies (no None case)
- Parameter changes to efficiency_threshold WILL change trade count
- Synthetic data will show different behavior when parameters change
- Real data gets more selective entry (better quality trades)

Expected: 400-550 trades on real data, clearly different from baseline on synthetic,
targeting -0.0180 to -0.0280.
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
        level_proximity: float = 0.5,
        min_wick_points: float = 0.50,
        min_body_points: float = 0.25,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        volume_multiplier: float = 1.5,
        efficiency_window: int = 10,
        efficiency_threshold: float = 1.2,
        min_baseline_efficiency: float = 0.5,
        max_level_age_bars: int = 8,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.min_body_points = min_body_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.volume_multiplier = volume_multiplier
        self.efficiency_window = efficiency_window
        self.efficiency_threshold = efficiency_threshold
        self.min_baseline_efficiency = min_baseline_efficiency
        self.max_level_age_bars = max_level_age_bars

        # Current session volume profile (0.25-point granularity)
        self._current_session_profile: dict[float, int] = {}
        self._current_session: Optional[int] = None
        
        # Track when each level was first visited (bar number in session)
        self._level_first_visit_bar: dict[float, int] = {}
        self._bars_in_session: int = 0
        
        # Track efficiency for filtering
        self._recent_efficiency: deque = deque(maxlen=efficiency_window)
        
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
            self._bars_in_session = 0

        self._bars_in_session += 1

        # Accumulate volume into current session profile
        self._update_session_profile(current)
        
        # Track level visits (freshness)
        self._record_level_visit(current)
        
        # Track efficiency metrics for filtering
        self._update_efficiency_metrics(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect and immediately enter on absorption with efficiency and freshness filters
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Handle session boundary - reset for new session."""
        self._current_session_profile = {}
        self._level_first_visit_bar = {}
        self._recent_efficiency = deque(maxlen=self.efficiency_window)
        self._entered_bar_timestamps = set()
        self._bars_in_session = 0

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

    def _record_level_visit(self, bar) -> None:
        """Record first visit bar number for levels near current price."""
        close_rounded = round(bar.close * 4) / 4
        
        # Record this level if not yet visited
        if close_rounded not in self._level_first_visit_bar:
            self._level_first_visit_bar[close_rounded] = self._bars_in_session

    def _update_efficiency_metrics(self, bar) -> None:
        """Track efficiency for absorption filtering."""
        # Calculate efficiency: body / abs(delta)
        if bar.delta != 0:
            body = abs(bar.close - bar.open)
            efficiency = body / abs(bar.delta)
            self._recent_efficiency.append(efficiency)

    def _get_significant_levels(self) -> list[float]:
        """Extract levels with above-average volume from current session."""
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

    def _get_avg_efficiency(self) -> float:
        """Get average efficiency from recent bars, with baseline fallback."""
        if not self._recent_efficiency:
            return self.min_baseline_efficiency
        return max(sum(self._recent_efficiency) / len(self._recent_efficiency),
                   self.min_baseline_efficiency)

    def _is_level_fresh(self, level: float) -> bool:
        """Check if a level was visited recently (within max_level_age_bars)."""
        if level not in self._level_first_visit_bar:
            return False
        
        first_visit = self._level_first_visit_bar[level]
        age = self._bars_in_session - first_visit
        
        return age <= self.max_level_age_bars

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at significant fresh levels with efficiency filter.
        
        Absorption requires:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Meaningful body (>= 0.25 points)
        3. Wick showing price rejection (>= 0.50 points)
        4. Proximity to significant level (within 0.5 point)
        5. Level is fresh (visited within last 8 bars)
        6. High efficiency: body/delta >= 1.2x recent average (ALWAYS APPLIED)
        7. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Meaningful body
        body = abs(current.close - current.open)
        if body < self.min_body_points:
            return None

        # Filter 3: Check proximity to significant level
        significant_levels = self._get_significant_levels()
        if not significant_levels:
            return None

        min_distance = min(abs(current.close - level) for level in significant_levels)
        if min_distance > self.level_proximity:
            return None

        # Find the nearest level
        nearest_level = min(significant_levels, key=lambda l: abs(current.close - l))

        # Filter 4: Level must be fresh
        if not self._is_level_fresh(nearest_level):
            return None

        # Filter 5: Efficiency check - ALWAYS APPLIED (no None case)
        if delta != 0:
            current_efficiency = body / abs(delta)
            avg_efficiency = self._get_avg_efficiency()
            
            # Hard filter: must meet efficiency threshold
            if current_efficiency < avg_efficiency * self.efficiency_threshold:
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
