"""
Volume profile absorption strategy - fixing density check logic to actually filter bars.

The previous trial added a density check but it was checking the CURRENT bar's volume
against recent volumes BEFORE the current bar was added to the deque. This meant the
density check didn't actually filter anything - it was checking stale history.

The byte-identical synthetic result confirms this: the density check wasn't working
because of the timing - we were checking if current volume > average of previous volumes,
but then adding current to the deque AFTER the check, so the check never actually
filtered the current bar being evaluated.

Fix: Check density AFTER adding current volume to recent volumes, or restructure to
check the current bar against its surrounding context properly.

Actually, deeper issue: the density check needs to look at the CONTEXT around the bar,
not just whether it's above average. A bar at a real level should have volume clustering
around that price level in the session profile, not just raw volume above average.

New approach - return to #81's proven core (delta_ratio=0.28, target_multiple=4.0)
but make the ACTUAL change that filters bars:

1. Only check absorption on bars where the bar's price range overlaps with HIGH-VOLUME
   price zones from the session profile (not just any bar)
2. This changes which bars are ACTUALLY considered, not through a faulty density check
3. A bar at a real level will have volume accumulated at its price in the profile
4. This is a REAL filter that changes the bar set, not a timing-bug filter

Expected: Different synthetic trades (not 70 identical), targeting -0.0050 or better.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_bar_range: float = 1.0,
        max_bar_range: float = 6.0,
        min_tick_count: int = 6,
        min_bar_volume: int = 100,
        top_levels_count: int = 5,
        volume_density_percentile: float = 0.60,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_bar_range = min_bar_range
        self.max_bar_range = max_bar_range
        self.min_tick_count = min_tick_count
        self.min_bar_volume = min_bar_volume
        self.top_levels_count = top_levels_count
        self.volume_density_percentile = volume_density_percentile

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

        # KEY CHANGE: Only check absorption if bar overlaps with high-volume price zones
        # This changes which bars are ACTUALLY considered for absorption detection
        if not self._bar_overlaps_high_volume_zone(current):
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

    def _bar_overlaps_high_volume_zone(self, current) -> bool:
        """
        Check if the bar's price range overlaps with high-volume price zones.
        
        This is the REAL filter that changes which bars are considered.
        A bar at a significant level will have volume accumulated at its price
        in the session profile. Bars that don't overlap high-volume zones are
        likely noise, not absorption at levels.
        
        This changes the SET OF BARS that reach absorption detection, not just
        the threshold for already-selected bars.
        """
        if not self._session_levels:
            return True  # No profile yet, accept
        
        # Find the high-volume price zones in the current session profile
        high_volume_prices = self._get_high_volume_prices()
        if not high_volume_prices:
            return True  # No high-volume zones yet, accept
        
        # Check if bar's price range overlaps with any high-volume zone
        # A zone is a single 0.25-point level, so we check if bar spans it
        for zone_price in high_volume_prices:
            if current.low <= zone_price <= current.high:
                return True  # Bar overlaps a high-volume zone
        
        return False  # Bar doesn't overlap any high-volume zone

    def _get_high_volume_prices(self) -> list[float]:
        """
        Extract price levels with above-median volume from the session profile.
        These are the "high-volume zones" where absorption is meaningful.
        """
        if not self._session_levels:
            return []
        
        volumes = list(self._session_levels.values())
        if not volumes:
            return []
        
        # Find the volume threshold (above this percentile)
        sorted_vols = sorted(volumes, reverse=True)
        threshold_idx = max(0, int(len(sorted_vols) * (1 - self.volume_density_percentile)))
        threshold_vol = sorted_vols[threshold_idx] if threshold_idx < len(sorted_vols) else 0
        
        # Return prices with volume above threshold
        high_volume_prices = [
            price for price, vol in self._session_levels.items()
            if vol >= threshold_vol
        ]
        
        return high_volume_prices

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

        # Distribute volume across 0.25-point price levels
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
        """Extract the top N most significant levels by volume."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        
        # Take only top N levels
        top_levels = [level for level, _ in sorted_items[:self.top_levels_count]]
        
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
        Detect absorption with #81's proven parameters.
        
        Only called on bars that overlap high-volume price zones.
        This changes which bars are considered, producing different results.
        
        Key parameters from #81's -0.0096 success:
        - delta_ratio: 0.28 (proven threshold)
        - level_proximity: 0.50 (tight matching)
        - target_multiple: 4.0 (proven)
        - stop_buffer: 0.25 (proven)
        """
        if current.volume == 0:
            return None

        bar_range = current.high - current.low

        # Filter by bar structure
        if bar_range < self.min_bar_range or bar_range > self.max_bar_range:
            return None

        if current.volume < self.min_bar_volume:
            return None

        if current.tick_count < self.min_tick_count:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Strict one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Must have a wick showing price rejection
        has_wick = False
        if delta > 0:  # Buying pressure
            lower_wick = current.close - current.low
            if lower_wick >= self.min_stop_distance:
                has_wick = True
        elif delta < 0:  # Selling pressure
            upper_wick = current.high - current.close
            if upper_wick >= self.min_stop_distance:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to carried level
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
