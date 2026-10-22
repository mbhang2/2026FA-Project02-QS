"""
Volume profile absorption strategy - refinement targeting improvement over #76's -0.0893.

Analysis of the plateau:
- #76 scored -0.0893 with 1079 trades (BEST SCORE)
- #74 scored -0.0980 with 2763 trades
- #16 scored -0.1076 with 463 trades

The campaign brief emphasizes that the question has two parts:
1. Does absorption at a carried level precede a move away from it?
2. Does waiting for initiation improve the outcome?

Current strategy only answers #1. The brief states: "A rule that acts on
absorption alone and a rule that waits for initiation are both answers, and so
is one that waits only under a stated condition. Waiting is **optional and it is
the point** — the campaign is trying to find out whether it helps."

Key insight: The brief explicitly asks for tagging entries by whether they were
taken on absorption alone or after a confirming bar. This allows measurement of
which approach works better WITHIN A SINGLE RULE.

This trial implements:
1. Keep #76's absorption detection (delta_ratio=0.28, level_proximity=6.0, 
   min_wick_points=-0.5, target_multiple=4.0)
2. Add optional initiation detection for bars that pass absorption
3. Track two entry types separately with different tags:
   - "absorption_only" - enter immediately when absorption detected
   - "absorption_initiated" - wait for a confirming bar showing efficiency
4. The initiation bar must show:
   - Signed flow in the direction of the absorption (not reversing)
   - Body size large relative to recent range
   - Efficiency (points traveled per unit of signed flow) above recent average

This approach directly addresses the campaign brief's requirement to measure
whether waiting helps, and allows the run record to show which entry type
performed better.

Expected: Score near or better than #76's -0.0893, with clear measurement of
initiation impact through separate tags.
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
        level_proximity: float = 6.0,
        min_wick_points: float = -0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        use_initiation: bool = True,
        efficiency_percentile: float = 0.70,
        body_ratio_threshold: float = 0.60,
        initiation_lookback: int = 10,
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
        self.use_initiation = use_initiation
        self.efficiency_percentile = efficiency_percentile
        self.body_ratio_threshold = body_ratio_threshold
        self.initiation_lookback = initiation_lookback

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        
        # Track absorption bars and pending initiations
        self._entered_bar_timestamps: set = set()
        self._pending_initiations: dict[int, dict] = {}  # ts -> absorption setup
        
        # Track efficiency history for initiation detection
        self._efficiency_history: deque = deque(maxlen=self.initiation_lookback)
        self._range_history: deque = deque(maxlen=self.initiation_lookback)

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
        
        # Track efficiency metrics for initiation detection
        self._update_efficiency_history(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check for initiation on pending absorption setups
        if self.use_initiation and self._pending_initiations:
            self._check_initiation(ctx, current)
        
        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            
            if self.use_initiation:
                # Hold for initiation confirmation
                self._pending_initiations[current.close_ts] = setup
            else:
                # Enter immediately on absorption
                self._place_entry(ctx, setup, "absorption_only")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and carry forward."""
        if self._session_levels:
            levels = self._extract_levels_from_profile(self._session_levels)
            self._prior_session_levels.append(levels)
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        self._session_levels = {}
        self._entered_bar_timestamps = set()
        self._pending_initiations = {}

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _update_efficiency_history(self, bar) -> None:
        """Track efficiency (points per unit flow) and range for initiation."""
        if bar.volume == 0:
            return
        
        # Track range for body ratio calculation
        bar_range = bar.high - bar.low
        self._range_history.append(bar_range)
        
        # Track efficiency: points traveled per unit of signed flow
        # For bars with near-zero flow, skip (as brief notes this makes efficiency undefined)
        delta = bar.delta
        if abs(delta) > 0:
            efficiency = bar_range / abs(delta)
            self._efficiency_history.append(efficiency)

    def _extract_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract the most significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        top_levels = [level for level, vol in sorted_items[:8]]
        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with #76's proven parameters.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require wick showing price rejection
        has_wick = False
        if delta > 0:
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
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
        if delta > 0:
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "delta": delta,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        else:
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "delta": delta,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

    def _check_initiation(self, ctx, current) -> None:
        """
        Check if current bar is a confirming/initiating bar for pending absorption.
        Initiation bar must show:
        - Flow in same direction as absorption
        - Large body relative to recent range
        - High efficiency (points per flow unit) relative to recent history
        """
        # Remove old pending setups (more than 3 bars old)
        ts_to_remove = []
        for ts, setup in self._pending_initiations.items():
            if current.close_ts - ts > 3 * 300_000_000_000:  # ~3 bars in nanoseconds
                ts_to_remove.append(ts)
        
        for ts in ts_to_remove:
            del self._pending_initiations[ts]
        
        # Check each pending absorption for initiation
        for absorption_ts, setup in list(self._pending_initiations.items()):
            if self._is_initiating_bar(current, setup):
                # Remove from pending and place entry with initiation tag
                del self._pending_initiations[absorption_ts]
                self._place_entry(ctx, setup, "absorption_initiated")
                return  # Only initiate one per callback

    def _is_initiating_bar(self, bar, absorption_setup) -> bool:
        """
        Check if this bar confirms the absorption signal.
        """
        if bar.volume == 0:
            return False
        
        direction = absorption_setup["direction"]
        absorption_delta = absorption_setup["delta"]
        
        # Flow must be in same direction as absorption
        if direction == "long" and bar.delta <= 0:
            return False
        if direction == "short" and bar.delta >= 0:
            return False
        
        # Body must be large relative to recent range
        if len(self._range_history) > 0:
            avg_range = sum(self._range_history) / len(self._range_history)
            if avg_range > 0:
                bar_body = abs(bar.close - bar.open)
                body_ratio = bar_body / avg_range
                if body_ratio < self.body_ratio_threshold:
                    return False
        
        # Efficiency must be above recent percentile
        if len(self._efficiency_history) >= 3:
            bar_efficiency = (bar.high - bar.low) / abs(bar.delta) if bar.delta != 0 else 0
            sorted_eff = sorted(self._efficiency_history)
            threshold_idx = max(0, int(len(sorted_eff) * self.efficiency_percentile) - 1)
            efficiency_threshold = sorted_eff[threshold_idx]
            if bar_efficiency < efficiency_threshold:
                return False
        
        return True

    def _place_entry(self, ctx, setup: dict, entry_type: str) -> None:
        """Place entry order on absorption detection."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=entry_type,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=entry_type,
            )
