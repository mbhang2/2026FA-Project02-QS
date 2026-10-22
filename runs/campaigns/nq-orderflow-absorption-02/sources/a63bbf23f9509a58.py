"""
Volume profile-based absorption strategy for order flow analysis.

This strategy implements the campaign's core investigation: does absorption at 
carried volume levels precede moves between those levels?

Key design decisions:
1. Build a rolling volume profile from the current and prior sessions
2. Identify price levels where significant volume traded (point of control, value area)
3. Detect when price arrives at a carried level with one-sided flow but fails to extend
4. Trade the anticipated move away from that absorption level
5. Tag entries to distinguish absorption-only from initiation-confirmed trades
6. Hold for multi-bar moves between profile levels rather than scalping

The strategy tracks volume accumulation across bars and maintains structural stops
at the absorption bar's extreme plus a fixed buffer, with targets set as a multiple
of the stop distance.
"""

from __future__ import annotations
from collections import defaultdict
from typing import Dict, List, Optional, Any


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        profile_sessions: int = 2,
        absorption_threshold: float = 0.7,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.0,
        initiation_efficiency_percentile: float = 0.75,
        initiation_body_ratio: float = 0.6,
        min_stop_distance: float = 0.5,
        max_stop_distance: float = 3.0,
    ) -> None:
        self.interval = interval
        self.profile_sessions = profile_sessions
        self.absorption_threshold = absorption_threshold
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.initiation_efficiency_percentile = initiation_efficiency_percentile
        self.initiation_body_ratio = initiation_body_ratio
        self.min_stop_distance = min_stop_distance
        self.max_stop_distance = max_stop_distance
        
        # Accumulate bars for profile building
        self.bars_by_session: Dict[int, List[Any]] = defaultdict(list)
        self.current_session: Optional[int] = None
        
        # Volume profile: price -> volume
        self.profile: Dict[float, int] = {}
        
        # Track recent bar efficiency for initiation confirmation
        self.bar_efficiencies: List[float] = []
        self.recent_range: List[float] = []
        
        # Entry tracking
        self.absorption_setup: Optional[Dict[str, Any]] = None
        self.waiting_for_initiation: bool = False
        self.bars_since_absorption_setup: int = 0

    def setup(self, ctx: Any) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx: Any) -> None:
        if not ctx.actionable:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        current_bar = history[-1]
        
        # Track session changes
        if current_bar.session != self.current_session:
            self.current_session = current_bar.session
            self.bars_by_session[current_bar.session] = []
            self.bars_since_absorption_setup = 0
        
        # Accumulate bars for profile building
        self.bars_by_session[current_bar.session].append(current_bar)
        self._update_profile()
        
        # Update efficiency tracking for initiation confirmation
        self._update_bar_efficiency(current_bar)
        
        # Increment bars since absorption if waiting
        if self.waiting_for_initiation:
            self.bars_since_absorption_setup += 1
        
        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return
        
        # Check for absorption setup
        absorption_signal: Optional[Dict[str, Any]] = self._detect_absorption(current_bar, history)
        
        if absorption_signal:
            self.absorption_setup = absorption_signal
            self.waiting_for_initiation = True
            self.bars_since_absorption_setup = 0
            return
        
        # Check if we're waiting for initiation confirmation
        if self.waiting_for_initiation and self.absorption_setup:
            if self._is_initiation_bar(current_bar):
                self._place_entry(ctx, current_bar, tag="initiation")
                self.absorption_setup = None
                self.waiting_for_initiation = False
            elif self.bars_since_absorption_setup > 3:
                # Give up waiting after 3 bars
                self.absorption_setup = None
                self.waiting_for_initiation = False

    def on_fill(self, ctx: Any) -> None:
        pass

    def on_lifecycle(self, ctx: Any) -> None:
        pass

    def _update_profile(self) -> None:
        """Update volume profile from recent sessions."""
        self.profile = {}
        
        # Include current and prior sessions in profile
        sessions_to_include: List[int] = []
        for session_idx in sorted(self.bars_by_session.keys()):
            if len(sessions_to_include) < self.profile_sessions:
                sessions_to_include.append(session_idx)
        
        # Build price-level volume map
        for session_idx in sessions_to_include:
            for bar in self.bars_by_session[session_idx]:
                # Distribute volume across price levels the bar touched
                if bar.high > bar.low and bar.volume > 0:
                    price_range = bar.high - bar.low
                    num_levels = max(1, int(price_range / 0.25))
                    vol_per_level = bar.volume / num_levels
                    
                    for i in range(num_levels):
                        level = round(bar.low + (i * price_range / num_levels), 2)
                        if level not in self.profile:
                            self.profile[level] = 0
                        self.profile[level] += int(vol_per_level)

    def _detect_absorption(self, current_bar: Any, history: Any) -> Optional[Dict[str, Any]]:
        """Detect absorption: one-sided flow at a profile level with failed extension."""
        if not self.profile or current_bar.volume == 0:
            return None
        
        # Find nearest profile level to current bar
        profile_prices = sorted(self.profile.keys())
        current_price = current_bar.close
        
        nearest_level = min(profile_prices, key=lambda p: abs(p - current_price), default=None)
        if nearest_level is None or abs(nearest_level - current_price) > 1.0:
            return None
        
        # Check for one-sided flow (absorption signature)
        delta = current_bar.delta
        total_volume = current_bar.volume
        
        if total_volume == 0:
            return None
        
        flow_ratio = abs(delta) / total_volume
        
        if flow_ratio < self.absorption_threshold:
            return None
        
        # Check for failed extension (tail formation)
        if len(history) < 2:
            return None
        
        prev_bar = history[-2]
        
        # Long absorption: strong buy flow but closes below previous bar's low
        if delta > 0 and current_bar.close < prev_bar.low:
            stop_price = current_bar.low - self.stop_buffer
            stop_distance = current_bar.close - stop_price
            if self.min_stop_distance <= stop_distance <= self.max_stop_distance:
                return {
                    "direction": "long",
                    "level": nearest_level,
                    "bar": current_bar,
                    "stop_price": stop_price,
                    "stop_distance": stop_distance,
                    "flow_strength": flow_ratio,
                }
        
        # Short absorption: strong sell flow but closes above previous bar's high
        if delta < 0 and current_bar.close > prev_bar.high:
            stop_price = current_bar.high + self.stop_buffer
            stop_distance = stop_price - current_bar.close
            if self.min_stop_distance <= stop_distance <= self.max_stop_distance:
                return {
                    "direction": "short",
                    "level": nearest_level,
                    "bar": current_bar,
                    "stop_price": stop_price,
                    "stop_distance": stop_distance,
                    "flow_strength": abs(flow_ratio),
                }
        
        return None

    def _update_bar_efficiency(self, bar: Any) -> None:
        """Track bar efficiency: points traveled per unit signed flow."""
        if bar.volume == 0:
            return
        
        if bar.delta == 0:
            efficiency = 0.0
        else:
            points_traveled = bar.high - bar.low
            efficiency = points_traveled / abs(bar.delta)
        
        self.bar_efficiencies.append(efficiency)
        if len(self.bar_efficiencies) > 20:
            self.bar_efficiencies.pop(0)
        
        # Track recent range for body size comparison
        self.recent_range.append(bar.high - bar.low)
        if len(self.recent_range) > 10:
            self.recent_range.pop(0)

    def _is_initiation_bar(self, bar: Any) -> bool:
        """Check if bar shows efficient movement confirming the absorption setup."""
        if not self.absorption_setup or not self.bar_efficiencies:
            return False
        
        if len(self.bar_efficiencies) < 5 or len(self.recent_range) < 5:
            return False
        
        # Calculate efficiency percentile
        sorted_eff = sorted(self.bar_efficiencies)
        threshold_idx = int(len(sorted_eff) * self.initiation_efficiency_percentile)
        efficiency_threshold = sorted_eff[threshold_idx]
        
        # Current bar efficiency
        if bar.delta == 0:
            return False
        
        current_efficiency = (bar.high - bar.low) / abs(bar.delta)
        
        # Check body size relative to recent range
        body = abs(bar.close - bar.open)
        avg_range = sum(self.recent_range) / len(self.recent_range)
        
        return (
            current_efficiency > efficiency_threshold and
            body > avg_range * self.initiation_body_ratio
        )

    def _place_entry(self, ctx: Any, bar: Any, tag: str) -> None:
        """Place entry order based on absorption setup."""
        if not self.absorption_setup:
            return
        
        setup = self.absorption_setup
        direction = setup["direction"]
        stop_distance = setup["stop_distance"]
        target_distance = stop_distance * self.target_multiple
        
        if direction == "long":
            ctx.buy(
                size=1,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=f"absorption-{tag}",
            )
        else:
            ctx.sell(
                size=1,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=f"absorption-{tag}",
            )
