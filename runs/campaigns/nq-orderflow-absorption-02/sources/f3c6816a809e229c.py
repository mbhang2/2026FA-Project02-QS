"""
Volume profile absorption strategy - test flow reversal and level visit count.

Analysis of persistent byte-identical issue:
The delta_consistency check in the previous trial still produced identical trades,
which means the synthetic data either:
1. Has very few bars where previous delta sign differs from current
2. The delta values are consistently in the same direction
3. The filter is too weak to eliminate enough bars

Key insight from trial history:
- #81 (best): -0.0096, 622 trades with delta_ratio=0.28, level_proximity=4.0
- #195 (positive): +0.0646, 535 trades with reversal confirmation
- #165: -0.0350, 658 trades

The fact that #195 achieved positive returns with a reversal-based approach suggests
that sometimes OPPOSING the absorption signal (not trading it) is better. This is
fundamentally different from filtering which absorptions to trade.

New hypothesis: The campaign asks "does absorption at a level precede a move away?"
The answer might be: "Only if the level itself is being visited for the first time
in this session, or has been visited very few times."

A level that has been hit many times is a range boundary, not a breakout point.
A level that is fresh (newly formed, visited once or twice) might have more predictive
power for moves away from it.

Implementation:
1. Track visit count for each level within the current session
2. Only trade absorption at levels with visit_count <= 2 (fresh levels)
3. A "visit" is when price closes within 0.25 points of the level
4. This directly tests whether fresh levels have different behavior than established ones

Additionally, implement a FLOW REVERSAL check:
- For long: require that the bar BEFORE the absorption bar had net sell flow (delta < 0)
  This means buying pressure is REVERSING previous selling, not continuing a trend
- For short: require that the bar BEFORE had net buy flow (delta > 0)
  This means selling pressure is REVERSING previous buying

This tests whether absorption that represents flow reversal (not continuation) is
the meaningful signal. It's the opposite of the previous delta_consistency check.

Expected: 300-500 trades with much more selective entry, targeting -0.0200 to -0.0400.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 4.0,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 2,
        concentration_threshold: float = 0.75,
        max_level_visits: int = 2,
        level_visit_tolerance: float = 0.25,
        require_flow_reversal: bool = True,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.rolling_window_sessions = rolling_window_sessions
        self.concentration_threshold = concentration_threshold
        self.max_level_visits = max_level_visits
        self.level_visit_tolerance = level_visit_tolerance
        self.require_flow_reversal = require_flow_reversal

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        
        # Track visit counts for levels in current session
        self._level_visit_counts: dict[float, int] = {}
        
        # Track the previous bar's delta for flow reversal checking
        self._previous_bar_delta: Optional[int] = None
        
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
        
        # Record this bar's close as a visit to nearby levels
        self._record_level_visits(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._previous_bar_delta = current.delta
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            self._previous_bar_delta = current.delta
            return

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)
        
        # Update previous delta for next iteration
        self._previous_bar_delta = current.delta

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Handle session boundary - add completed session to rolling window."""
        if self._current_session_profile:
            self._session_profiles.append(dict(self._current_session_profile))
            if len(self._session_profiles) > self.rolling_window_sessions:
                self._session_profiles.pop(0)
        
        self._current_session_profile = {}
        self._level_visit_counts = {}
        self._entered_bar_timestamps = set()
        self._previous_bar_delta = None

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

    def _record_level_visits(self, bar) -> None:
        """Record that price visited levels near the bar's close."""
        close_rounded = round(bar.close * 4) / 4
        
        # Check nearby levels within tolerance
        for level in list(self._level_visit_counts.keys()):
            if abs(bar.close - level) <= self.level_visit_tolerance:
                self._level_visit_counts[level] += 1
        
        # Also initialize the close level if not already tracked
        if close_rounded not in self._level_visit_counts:
            self._level_visit_counts[close_rounded] = 1

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract levels representing the top concentration_threshold of volume."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        cumulative_vol = 0
        selected_levels = []
        
        for level, vol in sorted_items:
            selected_levels.append(level)
            cumulative_vol += vol
            if cumulative_vol >= total_vol * self.concentration_threshold:
                break

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from the rolling window of sessions."""
        all_levels = set()
        
        # Add levels from current session
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        
        # Add levels from all prior sessions in the rolling window
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _get_level_visit_count(self, price: float) -> int:
        """Get visit count for a level near the given price."""
        price_rounded = round(price * 4) / 4
        return self._level_visit_counts.get(price_rounded, 0)

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels with flow reversal check.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Proximity to carried level (within 4.0 points)
        5. Level has been visited <= 2 times in current session (fresh level)
        6. (Optional) Previous bar's delta was opposite direction (flow reversal)
        7. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Filter 2: Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Filter 3: Check that the nearest level is fresh (not visited too many times)
        nearest_level = min(carried_levels, key=lambda l: abs(current.close - l))
        visit_count = self._get_level_visit_count(nearest_level)
        if visit_count > self.max_level_visits:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Check wick
            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None
            
            # Check wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Check flow reversal if enabled
            if self.require_flow_reversal:
                if self._previous_bar_delta is not None and self._previous_bar_delta >= 0:
                    # Previous bar had buy pressure or neutral - not a reversal
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
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Check wick
            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None
            
            # Check wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Check flow reversal if enabled
            if self.require_flow_reversal:
                if self._previous_bar_delta is not None and self._previous_bar_delta <= 0:
                    # Previous bar had sell pressure or neutral - not a reversal
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
