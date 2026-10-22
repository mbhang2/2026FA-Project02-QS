"""
Volume profile absorption strategy - test confirmation with prior bar delta reversal.

Analysis of byte-identical issue:

The synthetic data produces 70 trades identical to baseline. The confirmation logic
in the previous trial is checking the confirming bar's properties, but this is still
too weak because:

1. A bar with strong directional delta is likely to continue that direction
2. Checking the confirming bar's delta and range is just confirming the trend continues
3. This is almost always true on synthetic data, so it doesn't filter effectively

Root cause: The confirmation check is backwards. We should be checking if the
PREVIOUS bar (the one before absorption) had opposite flow. If absorption happens
after the market has already been moving in that direction, it's not meaningful
absorption - it's just continuation.

Real absorption is when:
- The market has been moving ONE direction (say, buying pressure)
- Price arrives at a level with OPPOSITE pressure (selling pressure)
- This opposite pressure fails to move price (absorption = wick rejection)
- Then the original direction resumes

This is the "flow reversal" hypothesis: absorption matters when it represents
flow switching direction, not when it's the same direction continuing.

Implementation:
1. Track the delta sign of the bar BEFORE the absorption bar
2. Only detect absorption when the absorption bar's delta is OPPOSITE to the prior bar
3. This means we're seeing a genuine reversal/absorption, not trend continuation

This should dramatically reduce trade count on synthetic data because:
- It adds a genuine constraint (prior bar must have opposite delta sign)
- It requires actual flow reversal, not just any absorption bar
- This constraint will filter out most synthetic trades

Expected: 200-300 trades on real data (much lower than 622), with synthetic showing
clearly different count, targeting -0.0300 to -0.0600.
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
        min_wick_points: float = 0.25,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.75,
        require_prior_reversal: bool = True,
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
        self.concentration_threshold = concentration_threshold
        self.require_prior_reversal = require_prior_reversal

        # Current session volume profile (0.25-point granularity)
        self._current_session_profile: dict[float, int] = {}
        self._current_session: Optional[int] = None
        
        # Track previous bar's delta for reversal detection
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
        """Handle session boundary - reset for new session."""
        self._current_session_profile = {}
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
        """Get significant levels from current session."""
        return self._extract_significant_levels(self._current_session_profile)

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels with prior bar reversal check.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.25 points)
        3. Proximity to carried level (within 4.0 points)
        4. (Optional) Prior bar had opposite delta sign (flow reversal)
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

        # LONG: Buying pressure
        if delta > 0:
            # Filter 3: Check for prior bar reversal (prior bar had selling pressure)
            if self.require_prior_reversal:
                if self._previous_bar_delta is not None and self._previous_bar_delta >= 0:
                    # Previous bar did not have selling pressure - not a reversal
                    return None
            
            # Check for lower wick (price rejection below)
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
            # Filter 3: Check for prior bar reversal (prior bar had buying pressure)
            if self.require_prior_reversal:
                if self._previous_bar_delta is not None and self._previous_bar_delta <= 0:
                    # Previous bar did not have buying pressure - not a reversal
                    return None
            
            # Check for upper wick (price rejection above)
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
