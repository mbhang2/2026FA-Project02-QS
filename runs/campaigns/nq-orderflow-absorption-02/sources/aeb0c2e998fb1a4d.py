"""
Volume profile absorption strategy - test bar efficiency confirmation.

Campaign analysis shows the byte-identical issue persists because the absorption
detection filters are stable. The key variable is the ENTRY TIMING, not the level
extraction or basic absorption detection.

Previous attempts at confirmation (checking next bar for directional follow-through)
produced byte-identical trades, suggesting the confirmation condition is either
too lenient or not actually filtering different bars on synthetic data.

New insight: The issue may be that we're queuing the setup for the NEXT bar, but
the synthetic data may not have enough bars to confirm. Alternatively, the
confirmation condition (close > absorption_close for long) is too simple and
nearly always true.

This trial removes the two-bar confirmation approach and instead focuses on
improving the IMMEDIATE entry filters:

1. Add BAR EFFICIENCY filter: bars with high delta ratio AND meaningful range
   - A truly absorbing bar should have both: strong directional flow AND price
     rejection (wick showing price didn't follow the flow)
   - Current approach checks these separately; combine them into one efficiency metric

2. Tighten delta_ratio to 0.32 (require very strong directional flow)

3. Require BOTH lower wick (for long) AND close near the low
   - This is the signature of absorption: price got pushed down despite buying
     pressure, then closed near the low (showing rejection of higher prices)
   - For short: upper wick AND close near the high

4. Increase min_wick_points to 0.50 (require significant price rejection)

5. Reduce target_multiple to 3.5 (better risk/reward)

6. Use 3-session rolling window instead of 2
   - This gives more carried levels to detect absorption at
   - More levels = more opportunities for the efficiency filters to trigger

The key difference: instead of waiting for a confirming bar (which may not exist
in synthetic data), we make the absorption bar itself MORE selective by requiring
both strong directional flow AND evidence of price rejection (wick + close position).

Expected: 500-700 trades with better quality, targeting score -0.0100 to -0.0200.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.32,
        level_proximity: float = 4.0,
        min_wick_points: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 3,
        concentration_threshold: float = 0.75,
        close_proximity_ratio: float = 0.4,
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
        self.close_proximity_ratio = close_proximity_ratio

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

        # Detect absorption at carried levels and enter immediately
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
        1. Strong directional delta (delta_ratio >= 0.32)
        2. Significant wick showing price rejection (>= 0.50 points)
        3. Close positioned to show price rejection
           - For long (buying pressure): close near the low (within 40% of range)
           - For short (selling pressure): close near the high (within 40% of range)
        4. Proximity to carried level (within 4.0 points)
        5. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta (0.32)
        if delta_ratio < self.absorption_delta_ratio:
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
            # Require significant lower wick
            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None
            
            # Require close near the low (price rejected higher prices)
            # Close should be in the lower 40% of the bar's range
            bar_range = current.high - current.low
            if bar_range > 0:
                close_position = (current.close - current.low) / bar_range
                if close_position > self.close_proximity_ratio:
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
            # Require significant upper wick
            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None
            
            # Require close near the high (price rejected lower prices)
            # Close should be in the upper 40% of the bar's range
            bar_range = current.high - current.low
            if bar_range > 0:
                close_position = (current.high - current.close) / bar_range
                if close_position > self.close_proximity_ratio:
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
