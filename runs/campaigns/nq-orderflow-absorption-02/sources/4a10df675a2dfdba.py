"""
Volume profile absorption strategy - test delta_ratio sensitivity and level concentration.

The byte-identical issue persists because the filters are too similar to #81.
Analysis shows #81 (score -0.0096, 622 trades) is the best performer, but we need
to understand WHY it works and what variations actually change behavior.

Key observation: All top performers use delta_ratio=0.28, but the synthetic data
byte-identical issue suggests the current implementation's filters aren't sensitive
enough to create variations.

New approach - test a different hypothesis about what makes absorption real:

1. Increase delta_ratio to 0.30 (require even stronger directional flow)
   - This should filter MORE bars, creating different entry count
   - If synthetic shows byte-identical, the delta_ratio filter isn't active

2. Change level_proximity from 4.0 to 5.0 (allow absorption further from levels)
   - This should allow more entries by being less strict about proximity
   - If synthetic shows byte-identical, proximity isn't the active filter

3. Tighten concentration_threshold from 0.75 to 0.70
   - Extract MORE levels (lower threshold = include more volume)
   - This directly affects _get_carried_levels() return value
   - More levels = more opportunities to be within level_proximity

4. Increase target_multiple from 4.0 to 4.5
   - This changes the bracket size, affecting risk/reward
   - Should show up in the blotter if the strategy is actually trading

5. Add a bar efficiency check: require wick to be at least 50% of bar range
   - For long: lower_wick >= 0.5 * bar_range
   - For short: upper_wick >= 0.5 * bar_range
   - This makes absorption detection more specific about price rejection

6. Reduce min_wick_points from 0.25 to 0.10
   - Allow detection of absorption on smaller bars
   - Counterbalanced by the wick-to-range ratio requirement

These changes are concrete enough to show different behavior on synthetic data
if the filters are actually working. If still byte-identical, it means the
absorption detection itself isn't triggering differently, and we need to
reconsider the entire approach.

Expected: 500-800 trades with different distribution, targeting -0.0100 to -0.0200.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        level_proximity: float = 5.0,
        min_wick_points: float = 0.10,
        wick_to_range_ratio: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 2,
        concentration_threshold: float = 0.70,
        min_close_ratio: float = 0.5,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_ratio = wick_to_range_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.rolling_window_sessions = rolling_window_sessions
        self.concentration_threshold = concentration_threshold
        self.min_close_ratio = min_close_ratio

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = -1
        self._current_session_profile: dict[float, int] = {}
        
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
        """Handle session boundary - add completed session to rolling window."""
        if self._current_session_profile:
            self._session_profiles.append(dict(self._current_session_profile))
            if len(self._session_profiles) > self.rolling_window_sessions:
                self._session_profiles.pop(0)
        
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

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.30)
        2. Wick showing price rejection (>= 0.10 points)
        3. Wick is substantial relative to bar range (>= 50%)
        4. Close above/below midpoint (showing recovery)
        5. Proximity to carried level (within 5.0 points)
        6. Valid stop/target distance
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

        # LONG: Buying pressure with absorption signature
        if delta > 0:
            # Require meaningful lower wick
            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None
            
            # Require wick to be substantial relative to bar range
            if lower_wick < self.wick_to_range_ratio * bar_range:
                return None
            
            # Require close above midpoint (recovery from wick)
            close_ratio = (current.close - current.low) / bar_range
            if close_ratio < self.min_close_ratio:
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
        
        # SHORT: Selling pressure with absorption signature
        elif delta < 0:
            # Require meaningful upper wick
            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None
            
            # Require wick to be substantial relative to bar range
            if upper_wick < self.wick_to_range_ratio * bar_range:
                return None
            
            # Require close below midpoint (recovery from wick)
            close_ratio = (current.high - current.close) / bar_range
            if close_ratio < self.min_close_ratio:
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
