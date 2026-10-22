"""
Volume profile absorption strategy - test directional confirmation with initiation bar.

Campaign analysis reveals a critical pattern:
- #81: -0.0096 with 622 trades (BEST OVERALL - extreme selectivity)
- #167: -0.0293 with 626 trades
- #165: -0.0350 with 658 trades

The campaign brief explicitly asks: "Does absorption at a carried level precede
a move away from that level?" and "Does waiting for initiation improve the outcome?"

Current approach (pure absorption detection) achieves -0.0096 by being extremely
selective. However, we're not addressing the initiation question at all. The brief
states we MUST report separately what waiting for initiation costs or earned.

This trial implements the two-tier entry system the campaign asks for:
1. ABSORPTION-ONLY entry: act immediately when absorption is detected
2. INITIATION entry: wait for a confirming bar that shows efficient movement

Key changes from current strategy:
- Add initiation detection: track bar efficiency (points per unit signed flow)
- Implement both entry paths with separate tags ("absorption" vs "initiation")
- Measure initiation bar's body size relative to recent range
- Keep absorption filters strict (delta_ratio=0.35, proximity=1.3)
- Add initiation efficiency threshold to require meaningful follow-through

The synthetic test will show MORE trades (because we now take both absorption
AND initiation entries), proving the change works. On real data, we'll see:
1. Which entry type performs better
2. What initiation costs in terms of timing
3. Whether the waiting actually improves outcomes

This directly answers the campaign's core question by tagging entries separately.
Expected: More trades than 70 on synthetic (proving initiation logic triggers),
and improved score through better entry timing on real data.
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        level_proximity: float = 1.3,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.52,
        max_prior_sessions: int = 5,
        efficiency_window: int = 20,
        initiation_efficiency_mult: float = 1.5,
        initiation_body_mult: float = 0.6,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.concentration_threshold = concentration_threshold
        self.max_prior_sessions = max_prior_sessions
        self.efficiency_window = efficiency_window
        self.initiation_efficiency_mult = initiation_efficiency_mult
        self.initiation_body_mult = initiation_body_mult

        # Level tracking with multi-session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        
        # Track absorption bars for initiation confirmation
        self._absorption_bar_ts: Optional[int] = None
        self._absorption_setup: Optional[dict] = None
        
        # Track bar efficiency for initiation detection
        self._bar_efficiencies: deque = deque(maxlen=self.efficiency_window)
        self._bar_ranges: deque = deque(maxlen=self.efficiency_window)

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

        # Track bar efficiency for initiation detection
        self._update_bar_efficiency(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check if we should enter on initiation (after absorption bar)
        if self._absorption_setup and self._absorption_bar_ts:
            if current.close_ts != self._absorption_bar_ts:
                initiation = self._detect_initiation(current, self._absorption_setup)
                if initiation:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._absorption_setup, tag="initiation")
                    self._absorption_setup = None
                    self._absorption_bar_ts = None
                    return
                else:
                    # Reset if initiation didn't confirm
                    self._absorption_setup = None
                    self._absorption_bar_ts = None

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            # Take absorption entry immediately
            self._place_entry(ctx, setup, tag="absorption")
            # Also store for potential initiation on next bar
            self._absorption_setup = setup
            self._absorption_bar_ts = current.close_ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and carry forward."""
        if self._session_levels:
            self._prior_session_levels.append(dict(self._session_levels))
            if len(self._prior_session_levels) > self.max_prior_sessions:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_bar_timestamps = set()
        self._absorption_setup = None
        self._absorption_bar_ts = None
        self._bar_efficiencies.clear()
        self._bar_ranges.clear()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels with 0.25-point granularity."""
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

    def _update_bar_efficiency(self, bar) -> None:
        """Track bar efficiency (points traveled per unit signed flow)."""
        if bar.volume <= 0:
            return
        
        # Bar range and body
        bar_range = bar.high - bar.low
        bar_body = abs(bar.close - bar.open)
        
        # Signed flow
        signed_flow = bar.delta
        
        # Efficiency: points per unit signed flow (avoid division by zero)
        if abs(signed_flow) > 0:
            efficiency = bar_range / abs(signed_flow)
        else:
            efficiency = 0.0
        
        self._bar_efficiencies.append(efficiency)
        self._bar_ranges.append(bar_range)

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract levels where volume is above significance threshold."""
        if not profile:
            return []

        max_vol = max(profile.values()) if profile else 0
        if max_vol == 0:
            return []

        threshold_vol = max_vol * self.concentration_threshold
        selected_levels = [level for level, vol in profile.items() if vol >= threshold_vol]

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = set()
        
        # Current session levels
        all_levels.update(self._extract_significant_levels(self._session_levels))
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption at carried levels."""
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Very strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Check proximity to carried level
        all_levels = self._get_carried_levels()
        if not all_levels:
            return None

        min_distance = min(abs(current.close - level) for level in all_levels)
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

    def _detect_initiation(self, current, absorption_setup: dict) -> bool:
        """
        Detect if current bar confirms initiation after absorption.
        
        Initiation bar should show:
        1. Efficient movement (high points per unit flow)
        2. Large body relative to recent range
        3. Direction matching the absorption setup
        """
        if current.volume == 0:
            return False
        
        direction = absorption_setup["direction"]
        bar_body = abs(current.close - current.open)
        bar_range = current.high - current.low
        signed_flow = current.delta
        
        # Check direction matches
        if direction == "long" and signed_flow <= 0:
            return False
        if direction == "short" and signed_flow >= 0:
            return False
        
        # Calculate efficiency for this bar
        if abs(signed_flow) > 0:
            bar_efficiency = bar_range / abs(signed_flow)
        else:
            return False
        
        # Compare to recent average efficiency
        if not self._bar_efficiencies:
            return False
        
        avg_efficiency = sum(self._bar_efficiencies) / len(self._bar_efficiencies)
        if avg_efficiency == 0:
            return False
        
        # Bar should be more efficient than average
        if bar_efficiency < avg_efficiency * self.initiation_efficiency_mult:
            return False
        
        # Check body size relative to recent range
        if not self._bar_ranges:
            return False
        
        avg_range = sum(self._bar_ranges) / len(self._bar_ranges)
        if bar_body < avg_range * self.initiation_body_mult:
            return False
        
        return True

    def _place_entry(self, ctx, setup: dict, tag: str = "absorption") -> None:
        """Place entry order with specified tag."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

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
