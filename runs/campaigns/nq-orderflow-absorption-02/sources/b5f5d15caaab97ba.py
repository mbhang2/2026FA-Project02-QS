"""
Volume profile absorption strategy - test initiation filter hypothesis.

Campaign analysis shows #81 achieved -0.0096 with 622 trades using immediate
entry on absorption. The campaign explicitly asks whether waiting for a
confirming bar (initiation) improves results.

Current hypothesis to test: Add an optional initiation filter that waits for
a bar showing efficient price movement relative to flow, but only apply it
selectively - on absorption bars where the signal is weaker (lower delta_ratio).

This creates two entry paths:
1. Strong absorption (delta_ratio >= 0.35): enter immediately, tag "absorption"
2. Moderate absorption (0.28 <= delta_ratio < 0.35): wait for initiation bar,
   tag "absorption_confirmed"

The initiation bar requires:
- Efficiency: points moved per unit of absolute flow > recent median
- Body size: bar body > 75% of recent average range
- Direction agreement: bar moves in direction of absorption flow

This tests whether initiation helps on weaker setups while preserving the
immediate entry on strong absorption that worked in #81.

The rolling_window_sessions parameter is increased from 2 to 3 to carry more
level history, giving more carried levels to detect absorption against.

Expected: Byte-different from #81 (new initiation logic), with 700-900 trades
and score -0.0050 to -0.0150 depending on whether initiation helps.
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
        strong_absorption_threshold: float = 0.35,
        level_proximity: float = 5.0,
        min_wick_points: float = -0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 3,
        concentration_threshold: float = 0.75,
        efficiency_percentile: float = 0.60,
        min_body_ratio: float = 0.75,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.strong_absorption_threshold = strong_absorption_threshold
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.rolling_window_sessions = rolling_window_sessions
        self.concentration_threshold = concentration_threshold
        self.efficiency_percentile = efficiency_percentile
        self.min_body_ratio = min_body_ratio

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        
        # Track pending initiations (absorption bars waiting for confirmation)
        self._pending_initiations: dict[int, dict] = {}
        
        # Track efficiency metrics for initiation filtering
        self._recent_efficiency: deque = deque(maxlen=20)
        self._recent_ranges: deque = deque(maxlen=20)

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
        
        # Update efficiency tracking
        self._update_efficiency_metrics(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check if any pending initiations are confirmed
        self._check_pending_initiations(ctx, current)

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            delta_ratio = setup.get("delta_ratio", 0)
            
            # Strong absorption: enter immediately
            if delta_ratio >= self.strong_absorption_threshold:
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, setup, tag_suffix="")
            else:
                # Moderate absorption: queue for initiation confirmation
                self._pending_initiations[current.close_ts] = setup

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
        
        # Reset for new session
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()
        self._pending_initiations = {}
        self._recent_efficiency = deque(maxlen=20)
        self._recent_ranges = deque(maxlen=20)

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

    def _update_efficiency_metrics(self, bar) -> None:
        """Track efficiency (points per volume) and range for initiation filtering."""
        if bar.volume <= 0:
            return
        
        # Efficiency: distance traveled per unit of flow
        distance = bar.high - bar.low
        efficiency = distance / bar.volume if bar.volume > 0 else 0
        self._recent_efficiency.append(efficiency)
        
        # Range for body size comparison
        range_val = bar.high - bar.low
        self._recent_ranges.append(range_val)

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract levels where volume is above concentration threshold."""
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

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption at carried levels."""
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
                "delta_ratio": delta_ratio,
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
                "delta_ratio": delta_ratio,
            }

    def _check_pending_initiations(self, ctx, current) -> None:
        """Check if any pending absorption bars are confirmed by initiation."""
        expired_keys = []
        
        for absorption_ts, setup in list(self._pending_initiations.items()):
            # Only check bars from previous interval (avoid same-bar confirmation)
            if current.close_ts <= absorption_ts:
                continue
            
            # Check initiation criteria
            if self._is_initiation_bar(current, setup):
                self._entered_bar_timestamps.add(absorption_ts)
                self._place_entry(ctx, setup, tag_suffix="_confirmed")
                expired_keys.append(absorption_ts)
            
            # Expire old pending initiations (after 5 bars)
            # This is a simple heuristic; could be refined
            elif current.close_ts - absorption_ts > 5 * 5 * 60 * 1_000_000_000:  # 5 bars at 5m
                expired_keys.append(absorption_ts)
        
        for key in expired_keys:
            del self._pending_initiations[key]

    def _is_initiation_bar(self, bar, absorption_setup) -> bool:
        """Check if bar shows efficient initiation in direction of absorption."""
        if not self._recent_efficiency or not self._recent_ranges:
            return False
        
        direction = absorption_setup["direction"]
        
        # Check efficiency: bar should be above median recent efficiency
        median_efficiency = sorted(self._recent_efficiency)[len(self._recent_efficiency) // 2]
        bar_efficiency = (bar.high - bar.low) / bar.volume if bar.volume > 0 else 0
        
        if bar_efficiency < median_efficiency * self.efficiency_percentile:
            return False
        
        # Check body size: should be large relative to recent range
        avg_range = sum(self._recent_ranges) / len(self._recent_ranges)
        body_size = abs(bar.close - bar.open)
        
        if body_size < avg_range * self.min_body_ratio:
            return False
        
        # Check direction agreement
        if direction == "long":
            return bar.close > bar.open  # Bullish body
        else:
            return bar.close < bar.open  # Bearish body

    def _place_entry(self, ctx, setup: dict, tag_suffix: str = "") -> None:
        """Place entry order on absorption detection."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]
        
        tag = f"absorption{tag_suffix}"

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
