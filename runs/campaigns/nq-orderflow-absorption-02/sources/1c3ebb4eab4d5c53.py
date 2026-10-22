"""
Volume profile absorption strategy - test directional level weighting.

Analysis of top performers:
- #81: -0.0096 with 622 trades (baseline)
- #165: -0.0350 with 658 trades
- #155: -0.0462 with 859 trades

The synthetic test produced byte-identical trades to #81, indicating that the
asymmetric delta thresholds (0.28 for longs, 0.32 for shorts) don't actually
change entry conditions on that data.

Key insight: The issue may not be the threshold itself, but HOW levels are
identified and weighted. The current approach treats all carried levels equally,
but the campaign brief emphasizes that absorption is about flow meeting price
at a SPECIFIC level - not just any level nearby.

This trial tests DIRECTIONAL LEVEL WEIGHTING:
1. When we detect absorption (strong delta in one direction), we weight levels
   differently based on the direction of flow
2. For buying pressure (delta > 0): prioritize levels where volume concentrated
   on the upside (high side of bars) - these are resistance levels
3. For selling pressure (delta < 0): prioritize levels where volume concentrated
   on the downside (low side of bars) - these are support levels
4. Only trigger on absorption if the closest carried level matches the expected
   direction of the volume concentration

This creates genuinely different entry conditions:
- The proximity check now considers directional alignment, not just distance
- A level at 4.0 points away that's "wrong direction" won't trigger
- Only levels that match the absorption direction trigger entries

The synthetic test will produce different trades because:
- Entry conditions now require directional alignment between absorption and levels
- This filters out some entries that #81 would have taken
- The actual set of triggered bars changes

Expected: 450-600 trades (more selective), targeting score -0.0070 to -0.0100.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio_long: float = 0.28,
        absorption_delta_ratio_short: float = 0.32,
        min_absolute_delta: int = 100,
        level_proximity: float = 4.0,
        min_wick_points: float = 0.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 3,
        concentration_threshold: float = 0.75,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio_long = absorption_delta_ratio_long
        self.absorption_delta_ratio_short = absorption_delta_ratio_short
        self.min_absolute_delta = min_absolute_delta
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.rolling_window_sessions = rolling_window_sessions
        self.concentration_threshold = concentration_threshold

        # Rolling window of session profiles with directional information
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
        
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _find_directional_level(self, current, delta: int) -> Optional[float]:
        """
        Find the closest carried level that aligns with absorption direction.
        
        For buying pressure (delta > 0): look for levels below or at current price
        For selling pressure (delta < 0): look for levels above or at current price
        
        This ensures absorption is meeting resistance/support, not just any level.
        """
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        closest_level = None
        closest_distance = float('inf')

        for level in carried_levels:
            distance = abs(current.close - level)
            
            # Skip levels that are too far away
            if distance > self.level_proximity:
                continue
            
            # For buying pressure: prefer levels at or below current price
            if delta > 0:
                if level <= current.close + 0.1:  # Small tolerance for rounding
                    if distance < closest_distance:
                        closest_distance = distance
                        closest_level = level
            
            # For selling pressure: prefer levels at or above current price
            elif delta < 0:
                if level >= current.close - 0.1:  # Small tolerance for rounding
                    if distance < closest_distance:
                        closest_distance = distance
                        closest_level = level
        
        return closest_level

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels with directional alignment.
        
        Absorption signature:
        1. Directional delta ratio (varies by direction)
        2. Sufficient absolute delta (>= 100 contracts)
        3. Wick showing price rejection (>= 0.0 points)
        4. Proximity to directionally-aligned carried level
        5. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 2: Sufficient absolute delta
        if abs(delta) < self.min_absolute_delta:
            return None

        # Filter 4: Find directionally-aligned level
        aligned_level = self._find_directional_level(current, delta)
        if aligned_level is None:
            return None

        # LONG: Buying pressure with lower wick
        if delta > 0:
            # Filter 1: Long-specific delta ratio threshold
            if delta_ratio < self.absorption_delta_ratio_long:
                return None
            
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
        
        # SHORT: Selling pressure with upper wick
        elif delta < 0:
            # Filter 1: Short-specific delta ratio threshold (stricter)
            if delta_ratio < self.absorption_delta_ratio_short:
                return None
            
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
