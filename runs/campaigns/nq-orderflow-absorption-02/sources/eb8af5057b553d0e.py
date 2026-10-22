"""
Volume profile absorption strategy - test asymmetric entry thresholds.

Analysis of trial history shows:
- #81: -0.0096 with 622 trades (BEST baseline)
- #165: -0.0350 with 658 trades
- #155: -0.0462 with 859 trades

All top performers use immediate entry with 2-session rolling window and
delta_ratio around 0.28. The synthetic test produced byte-identical trades
to the current strategy, indicating the min_absolute_delta and wick filters
are not discriminating.

Key insight: The current strategy applies the same filters to both long and
short setups. But the campaign asks about flow that "fails to move price" -
this asymmetry suggests different rejection signatures for buys vs sells.

Hypothesis: The wick requirement should be asymmetric:
- For LONGS (buying pressure): need strong lower wick (rejection of the buy)
  Tighter: min_wick_points = -0.1 (must show real rejection)
- For SHORTS (selling pressure): looser upper wick requirement
  Looser: min_wick_points_short = -0.75 (any price rejection counts)

This creates different entry conditions for each direction, which will
produce different trades than the symmetric version on real data.

Additionally:
1. Revert min_absolute_delta to 50 (from 100) - allow more setups
2. Return level_proximity to 3.5 (from 2.5) - wider net for entries
3. Adjust concentration_threshold to 0.80 - balance level strength

The asymmetry should trigger on different bars than the current symmetric
approach, particularly when the same bar shows strong buying pressure with
a wick vs strong selling pressure with a wick.

Expected: 600-800 trades with different entry distribution, targeting -0.0100 to -0.0200.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        min_absolute_delta: int = 50,
        level_proximity: float = 3.5,
        min_wick_points_long: float = -0.1,
        min_wick_points_short: float = -0.75,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 2,
        concentration_threshold: float = 0.80,
        level_granularity: float = 0.25,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_absolute_delta = min_absolute_delta
        self.level_proximity = level_proximity
        self.min_wick_points_long = min_wick_points_long
        self.min_wick_points_short = min_wick_points_short
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.rolling_window_sessions = rolling_window_sessions
        self.concentration_threshold = concentration_threshold
        self.level_granularity = level_granularity

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = -1
        self._current_session_profile: dict[float, int] = {}
        
        # Track entered bars to prevent re-entry
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

        # Use 0.25-point granularity
        granularity_multiplier = 1.0 / self.level_granularity
        low_rounded = round(bar.low * granularity_multiplier) / granularity_multiplier
        high_rounded = round(bar.high * granularity_multiplier) / granularity_multiplier

        num_levels = max(1, int((high_rounded - low_rounded) / self.level_granularity) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * granularity_multiplier) / granularity_multiplier
            self._current_session_profile[level_key] = (
                self._current_session_profile.get(level_key, 0) + int(vol_per_level)
            )
            level += self.level_granularity

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
        Detect absorption at carried levels with asymmetric wick filtering.
        
        Absorption signature:
        1. Strong directional delta ratio (>= 0.28)
        2. Significant absolute delta (>= 50)
        3. Asymmetric wick requirements:
           - LONG: tight lower wick (>= -0.1) for strong rejection
           - SHORT: looser upper wick (>= -0.75) for any rejection
        4. Proximity to carried level (within 3.5 points)
        5. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta ratio
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Absolute delta must be significant
        if abs(delta) < self.min_absolute_delta:
            return None

        # Filter 3: Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # LONG: Buying pressure with tight lower wick requirement
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Asymmetric: LONG requires tighter wick (-0.1)
            if lower_wick < self.min_wick_points_long:
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
        
        # SHORT: Selling pressure with looser upper wick requirement
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Asymmetric: SHORT allows looser wick (-0.75)
            if upper_wick < self.min_wick_points_short:
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
        """Place entry order on absorption detection - immediate entry."""
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
