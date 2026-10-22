"""
Volume profile absorption strategy - explore initiation logic as per campaign spec.

The campaign explicitly asks: "Does waiting for initiation improve the outcome?"

Best performer without initiation:
- #81: -0.0096 with 622 trades

The campaign spec defines initiation as a confirming bar that:
1. Moves price efficiently relative to flow (high flow efficiency)
2. Has a large body relative to recent range

This trial implements the initiation check as an optional filter:
- Detect absorption at carried level (same as #81)
- Wait for the next bar to confirm with initiation signal
- Only enter if initiation bar shows:
  * Efficiency: points_moved / abs(signed_flow) > recent_efficiency_median
  * Body size: bar_body > recent_range_average * 0.6

Key parameters from #81 (proven):
- absorption_delta_ratio: 0.28
- level_proximity: 5.0
- min_wick_points: -0.5
- target_multiple: 4.0

New initiation parameters (conservative):
- efficiency_lookback: 20 bars (rolling window for efficiency baseline)
- body_ratio_threshold: 0.6 (body must be 60% of recent range)
- wait_bars_max: 3 (don't wait more than 3 bars for initiation)

The hypothesis: Initiation filtering will reduce trade count but improve quality,
potentially improving the score by being more selective about which absorptions
actually precede moves.

Expected: Fewer trades (400-500) but better quality, targeting score -0.0050 to -0.0080.
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 5.0,
        min_wick_points: float = -0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 2,
        top_levels_count: int = 8,
        efficiency_lookback: int = 20,
        body_ratio_threshold: float = 0.6,
        wait_bars_max: int = 3,
        use_initiation: bool = True,
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
        self.top_levels_count = top_levels_count
        self.efficiency_lookback = efficiency_lookback
        self.body_ratio_threshold = body_ratio_threshold
        self.wait_bars_max = wait_bars_max
        self.use_initiation = use_initiation

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        
        # Pending absorption setups waiting for initiation
        self._pending_setups: dict[float, dict] = {}  # level -> setup info
        self._pending_age: dict[float, int] = {}  # level -> bars waited
        
        # Efficiency tracking
        self._efficiency_history: deque = deque(maxlen=efficiency_lookback)
        self._range_history: deque = deque(maxlen=efficiency_lookback)

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

        # Update efficiency and range history
        self._update_efficiency_history(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Process pending setups - check for initiation
        if self.use_initiation:
            self._process_pending_setups(ctx, current, history)

        # Detect new absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            if self.use_initiation:
                # Store for initiation check on next bar
                level = setup["level"]
                self._pending_setups[level] = setup
                self._pending_age[level] = 0
            else:
                # Immediate entry (like #81)
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
        self._pending_setups = {}
        self._pending_age = {}
        self._efficiency_history.clear()
        self._range_history.clear()

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

    def _update_efficiency_history(self, bar) -> None:
        """Track efficiency and range for initiation baseline."""
        if bar.volume <= 0:
            return

        # Efficiency: points moved per unit of signed flow
        points_moved = abs(bar.close - bar.open)
        signed_flow = bar.delta
        
        if signed_flow != 0:
            efficiency = points_moved / abs(signed_flow)
            self._efficiency_history.append(efficiency)
        
        # Range for body size comparison
        bar_range = bar.high - bar.low
        if bar_range > 0:
            self._range_history.append(bar_range)

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract the top N price levels by volume."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        top_levels = [level for level, _ in sorted_items[:self.top_levels_count]]
        
        return sorted(top_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from the rolling window of sessions."""
        all_levels = set()
        
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _get_median_efficiency(self) -> float:
        """Get median efficiency from history."""
        if not self._efficiency_history:
            return 0.5
        
        sorted_eff = sorted(self._efficiency_history)
        mid = len(sorted_eff) // 2
        if len(sorted_eff) % 2 == 0:
            return (sorted_eff[mid-1] + sorted_eff[mid]) / 2
        return sorted_eff[mid]

    def _get_average_range(self) -> float:
        """Get average range from history."""
        if not self._range_history:
            return 1.0
        
        return sum(self._range_history) / len(self._range_history)

    def _check_initiation(self, bar) -> bool:
        """Check if a bar shows initiation characteristics."""
        if not self._efficiency_history or not self._range_history:
            return False

        # Check efficiency
        points_moved = abs(bar.close - bar.open)
        signed_flow = bar.delta
        
        if signed_flow == 0:
            return False
        
        efficiency = points_moved / abs(signed_flow)
        median_eff = self._get_median_efficiency()
        
        if efficiency < median_eff:
            return False

        # Check body size
        bar_body = abs(bar.close - bar.open)
        avg_range = self._get_average_range()
        
        if bar_body < avg_range * self.body_ratio_threshold:
            return False

        return True

    def _process_pending_setups(self, ctx, current, history) -> None:
        """Check pending setups for initiation confirmation."""
        levels_to_remove = []
        
        for level, setup in self._pending_setups.items():
            self._pending_age[level] += 1
            
            # Check if initiation bar arrived
            if self._check_initiation(current):
                # Confirm the setup and enter
                self._place_entry(ctx, setup)
                levels_to_remove.append(level)
            elif self._pending_age[level] >= self.wait_bars_max:
                # Timeout - give up on this setup
                levels_to_remove.append(level)
        
        # Clean up
        for level in levels_to_remove:
            del self._pending_setups[level]
            del self._pending_age[level]

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels with #81's proven filters.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Evidence of price rejection via wick
        has_valid_wick = False
        if delta > 0:
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_valid_wick = True
        elif delta < 0:
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
        closest_level = min(carried_levels, key=lambda l: abs(l - current.close))
        
        if min_distance > self.level_proximity:
            return None

        # Calculate stop and target
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "level": closest_level,
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
                "level": closest_level,
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
