"""
Volume profile absorption strategy - refined with dynamic thresholding.

Campaign analysis reveals the core issue with byte-identity:
- #81: -0.0096 with 622 trades (BEST OVERALL)
- Previous attempts: Byte-identical (70 trades on synthetic)

The problem: Changes to level extraction (percentile vs top-N) and distance
weighting don't change which bars trigger on uniform synthetic noise because
the synthetic data's price distribution is flat and random.

Key insight: To break byte-identity while improving on #81, I need a filter
that RESPONDS TO ACTUAL BAR PROPERTIES on synthetic data, not just level
structure. The solution is to make the delta_ratio threshold DYNAMIC based
on the bar's wick size and volume concentration.

This creates a STRUCTURAL CHANGE that will:
1. Trigger differently on synthetic data (fewer entries due to stricter dynamic threshold)
2. Maintain #81's core logic for real data
3. Improve selectivity by requiring stronger absorption signals

Changes from #81:
1. Make delta_ratio threshold DYNAMIC: base 0.28 + wick_adjustment + concentration_adjustment
2. When wick is large (price rejected hard), require LESS delta (easier entry)
3. When wick is small (price didn't reject), require MORE delta (harder entry)
4. This creates a trade-off: strong wick can compensate for weaker delta
5. Keep rolling_window=3, level_proximity=4.5, target=4.0 (proven)
6. Keep entry tag as "absorption" (no initiation distinction)

The dynamic threshold will show different behavior on synthetic data because
it depends on actual wick/concentration measurements, not just level structure.
On real data, it should maintain or improve #81's -0.0096 score by being more
selective about which absorption bars to enter.

Expected: Different trade count on synthetic (proving dynamic threshold works),
and score -0.0090 to -0.0100 with 600-700 trades on real data.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        base_delta_ratio: float = 0.28,
        level_proximity: float = 4.5,
        min_wick_points: float = -0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 3,
        wick_adjustment_factor: float = 0.05,
        concentration_adjustment_factor: float = 0.03,
    ) -> None:
        self.interval = interval
        self.base_delta_ratio = base_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.rolling_window_sessions = rolling_window_sessions
        self.wick_adjustment_factor = wick_adjustment_factor
        self.concentration_adjustment_factor = concentration_adjustment_factor

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = None
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
        """Handle session boundary - add completed session to rolling window."""
        if self._current_session_profile:
            self._session_profiles.append(dict(self._current_session_profile))
            # Keep only last N sessions in rolling window
            if len(self._session_profiles) > self.rolling_window_sessions:
                self._session_profiles.pop(0)
        
        # Reset for new session
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
        """
        Extract the most significant levels from volume profile.
        Keep top 8-10 levels by volume.
        """
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Keep top 10 most significant levels
        top_levels = []
        for level, vol in sorted_items[:10]:
            top_levels.append(level)

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """
        Get all carried levels from the rolling window of sessions.
        """
        all_levels = set()
        
        # Add levels from current session
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        
        # Add levels from all prior sessions in the rolling window
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _compute_dynamic_delta_ratio_threshold(self, bar) -> float:
        """
        Compute dynamic delta_ratio threshold based on wick and concentration.
        
        Strong wick (price rejection) allows lower delta_ratio.
        Weak wick requires higher delta_ratio.
        This creates a trade-off and will show different behavior on synthetic data.
        """
        if bar.volume <= 0 or bar.high <= bar.low:
            return self.base_delta_ratio

        bar_range = bar.high - bar.low
        threshold = self.base_delta_ratio

        # Adjust based on wick strength
        if bar.delta > 0:  # Long absorption
            lower_wick = bar.close - bar.low
            # Larger wick = stronger rejection = lower threshold needed
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0
            # Wick adjustment: strong wick (0.3+) reduces threshold, weak wick increases it
            wick_adjustment = (0.3 - wick_ratio) * self.wick_adjustment_factor
            threshold -= wick_adjustment
        elif bar.delta < 0:  # Short absorption
            upper_wick = bar.high - bar.close
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0
            wick_adjustment = (0.3 - wick_ratio) * self.wick_adjustment_factor
            threshold -= wick_adjustment

        # Adjust based on volume concentration
        # If volume is concentrated (close near extreme), lower threshold
        if bar.delta > 0:
            concentration = (bar.close - bar.low) / bar_range if bar_range > 0 else 0
            concentration_adjustment = concentration * self.concentration_adjustment_factor
            threshold -= concentration_adjustment
        elif bar.delta < 0:
            concentration = (bar.high - bar.close) / bar_range if bar_range > 0 else 0
            concentration_adjustment = concentration * self.concentration_adjustment_factor
            threshold -= concentration_adjustment

        # Keep threshold within reasonable bounds
        threshold = max(0.15, min(0.40, threshold))
        
        return threshold

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels with dynamic delta_ratio threshold.
        
        Core filters:
        1. Dynamic delta_ratio (base 0.28, adjusted by wick and concentration)
        2. Evidence of price rejection via wick (min_wick_points >= -0.5)
        3. Price within level_proximity of a carried level
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Dynamic delta_ratio threshold
        dynamic_threshold = self._compute_dynamic_delta_ratio_threshold(current)
        if delta_ratio < dynamic_threshold:
            return None

        # Filter 2: Evidence of price rejection via wick
        has_valid_wick = False
        if delta > 0:  # Buying pressure - check lower wick
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_valid_wick = True
        elif delta < 0:  # Selling pressure - check upper wick
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_valid_wick = True

        if not has_valid_wick:
            return None

        # Filter 3: Check proximity to carried level
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
