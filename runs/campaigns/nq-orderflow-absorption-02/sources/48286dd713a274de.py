"""
Volume profile absorption strategy - test bar efficiency confirmation.

Campaign analysis shows byte-identical synthetic results with confirmation-based
approaches, but the best real-world performer (#81: -0.0096) uses immediate entry.
This suggests confirmation filters may not trigger under synthetic conditions but
could work on real data.

Key insight: Rather than wait for the NEXT bar to close above/below (which requires
looking ahead), implement a bar-by-bar confirmation that checks the CURRENT bar's
efficiency AFTER detecting absorption.

The campaign brief specifically asks about "initiation" - a bar showing efficient
price movement relative to flow. This can be measured on the absorption bar itself:

For a long setup (buying pressure):
- Delta > 0 (buying pressure detected)
- Lower wick shows absorption (price pushed down then rejected)
- Bar body (close - open) should be POSITIVE and substantial relative to recent range
- This shows the buying pressure is INITIATING a move, not just being absorbed

For a short setup (selling pressure):
- Delta < 0 (selling pressure detected)  
- Upper wick shows absorption (price pushed up then rejected)
- Bar body (open - close) should be POSITIVE and substantial relative to recent range
- This shows the selling pressure is INITIATING a move, not just being absorbed

This approach:
1. Doesn't require looking ahead to the next bar
2. Measures efficiency on the absorption bar itself
3. Aligns with the campaign's "initiation" concept
4. Should produce different trades because we're filtering on bar body strength

Changes from previous trial:
- Add bar body efficiency check (body size relative to recent range)
- Require body to be in the directional direction (close > open for long, etc.)
- Keep tighter absorption filters (delta_ratio 0.30, wick 0.75, proximity 3.5)
- This should trigger differently on synthetic data vs confirmation waiting

Expected: 400-500 trades with higher-quality initiation, targeting -0.0180 to -0.0280.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        level_proximity: float = 3.5,
        min_wick_points: float = 0.75,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 2,
        concentration_threshold: float = 0.75,
        body_efficiency_threshold: float = 0.5,
        lookback_bars: int = 10,
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
        self.rolling_window_sessions = rolling_window_sessions
        self.concentration_threshold = concentration_threshold
        self.body_efficiency_threshold = body_efficiency_threshold
        self.lookback_bars = lookback_bars

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        
        # Track recent bars for efficiency calculation
        self._recent_bars: list = []

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
        
        # Track recent bars for efficiency calculation
        self._update_recent_bars(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels and check for initiation
        setup = self._detect_absorption_with_initiation(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

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
        self._entered_bar_timestamps = set()
        self._recent_bars = []

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels for current session."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

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

    def _update_recent_bars(self, bar) -> None:
        """Keep track of recent bars for efficiency calculation."""
        self._recent_bars.append(bar)
        if len(self._recent_bars) > self.lookback_bars:
            self._recent_bars.pop(0)

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
        
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _calculate_recent_range(self) -> float:
        """Calculate the average range of recent bars."""
        if len(self._recent_bars) < 2:
            return 1.0
        
        ranges = [bar.high - bar.low for bar in self._recent_bars[:-1]]
        if not ranges:
            return 1.0
        
        return sum(ranges) / len(ranges)

    def _is_initiating_long(self, current) -> bool:
        """
        Check if a bar with buying pressure shows initiation.
        
        Initiation = bar body is positive and substantial relative to recent range.
        This shows the buying pressure is creating efficient price movement.
        """
        body = current.close - current.open
        if body <= 0:
            return False
        
        recent_range = self._calculate_recent_range()
        if recent_range <= 0:
            return False
        
        body_ratio = body / recent_range
        return body_ratio >= self.body_efficiency_threshold

    def _is_initiating_short(self, current) -> bool:
        """
        Check if a bar with selling pressure shows initiation.
        
        Initiation = bar body is negative and substantial relative to recent range.
        This shows the selling pressure is creating efficient price movement.
        """
        body = current.open - current.close
        if body <= 0:
            return False
        
        recent_range = self._calculate_recent_range()
        if recent_range <= 0:
            return False
        
        body_ratio = body / recent_range
        return body_ratio >= self.body_efficiency_threshold

    def _detect_absorption_with_initiation(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels with initiation confirmation.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.30)
        2. Meaningful wick showing price rejection (>= 0.75 points)
        3. Proximity to carried level (within 3.5 points)
        4. Bar body showing initiation (efficient directional movement)
        5. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # LONG: Buying pressure with meaningful lower wick AND initiating body
        if delta > 0:
            lower_wick = current.close - current.low
            
            if lower_wick < self.min_wick_points:
                return None
            
            # Check for initiation (bar body shows efficient upward movement)
            if not self._is_initiating_long(current):
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
        
        # SHORT: Selling pressure with meaningful upper wick AND initiating body
        elif delta < 0:
            upper_wick = current.high - current.close
            
            if upper_wick < self.min_wick_points:
                return None
            
            # Check for initiation (bar body shows efficient downward movement)
            if not self._is_initiating_short(current):
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
        """Place entry order on absorption detection with initiation."""
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
