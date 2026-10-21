"""
Volume profile absorption strategy: detect when aggressive flow arrives at a 
carried volume level but fails to move price (absorption), then trade the move 
away from that level.

Key elements:
1. Build and carry volume profiles from recent sessions
2. Identify price levels where significant volume traded
3. Detect absorption: flow arrives but price closes back past the previous extreme
4. Enter on absorption with structural stop just beyond the absorption bar
5. Hold for a move between profile levels
6. Optionally wait for a confirming bar showing efficient price movement

The strategy accumulates:
- Session-level volume profiles (price -> volume mapping)
- Carried levels from prior two sessions
- Recent bar history to detect absorption patterns
- Flow efficiency metrics for confirmation logic

Tag entries as "absorption" or "absorption_confirmed" to measure the cost/benefit
of waiting for initiation.
"""

from __future__ import annotations
from collections import defaultdict
from typing import Optional, Dict, List, Tuple, Any


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        sessions_to_carry: int = 2,
        min_volume_percentile: float = 0.70,
        absorption_flow_ratio: float = 0.3,
        efficiency_threshold: float = 1.5,
        efficiency_window: int = 10,
        target_risk_multiple: float = 2.0,
        max_stop_distance: float = 2.0,
        confirm_bars: int = 0,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.sessions_to_carry = sessions_to_carry
        self.min_volume_percentile = min_volume_percentile
        self.absorption_flow_ratio = absorption_flow_ratio
        self.efficiency_threshold = efficiency_threshold
        self.efficiency_window = efficiency_window
        self.target_risk_multiple = target_risk_multiple
        self.max_stop_distance = max_stop_distance
        self.confirm_bars = confirm_bars
        self.size = size

        # Session tracking
        self._current_session: Optional[int] = None
        self._session_profiles: Dict[int, Dict[float, int]] = {}
        self._carried_levels: List[Tuple[float, int]] = []
        
        # Bar history for absorption detection
        self._bar_history: List[Any] = []
        
        # Efficiency tracking for confirmation
        self._efficiencies_up: List[float] = []
        self._efficiencies_down: List[float] = []
        
        # Absorption detection state
        self._pending_absorption: Optional[Tuple[float, str, Any]] = None

    def setup(self, ctx: Any) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx: Any) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if not history:
            return

        # Update session and profiles
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._session_profiles[ctx.session] = {}
            self._update_carried_levels(ctx.session)

        # Process the most recent bar
        last = history[-1]
        self._update_session_profile(ctx.session, last)
        self._update_bar_history(last)
        self._update_efficiency_metrics(last)

        # Check for existing position or working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Detect absorption at a carried level
        absorption = self._detect_absorption(last)
        if absorption:
            level, direction = absorption
            if self.confirm_bars > 0:
                # Wait for confirmation
                self._pending_absorption = (level, direction, last)
            else:
                # Enter on absorption alone
                self._place_entry(ctx, level, direction, "absorption")
        
        # Check if pending absorption now has confirmation
        if self._pending_absorption:
            level, direction, absorption_bar = self._pending_absorption
            if self._is_confirming_bar(last, direction):
                self._place_entry(ctx, level, direction, "absorption_confirmed")
                self._pending_absorption = None

    def on_fill(self, ctx: Any) -> None:
        pass

    def on_lifecycle(self, ctx: Any) -> None:
        pass

    def _update_session_profile(self, session: int, bar: Any) -> None:
        """Add bar's volume to the price levels it touched."""
        if session not in self._session_profiles:
            self._session_profiles[session] = {}
        
        profile = self._session_profiles[session]
        # Distribute bar volume across price levels touched
        step = 0.25  # NQ tick size
        price = bar.low
        num_levels = max(1, int((bar.high - bar.low) / step) + 1)
        volume_per_level = bar.volume // num_levels
        
        while price <= bar.high:
            rounded_price = round(price, 2)
            profile[rounded_price] = profile.get(rounded_price, 0) + volume_per_level
            price += step

    def _update_carried_levels(self, current_session: int) -> None:
        """Maintain levels carried from recent sessions."""
        self._carried_levels = []
        
        # Look back at prior sessions
        for session_offset in range(1, self.sessions_to_carry + 1):
            session_idx = current_session - session_offset
            if session_idx not in self._session_profiles:
                continue
            
            profile = self._session_profiles[session_idx]
            if not profile:
                continue
            
            # Find the volume at each price level
            volumes = sorted(profile.items(), key=lambda x: x[1], reverse=True)
            if not volumes:
                continue
            
            # Get levels above the percentile threshold
            max_vol = volumes[0][1]
            threshold = max_vol * (1 - self.min_volume_percentile)
            
            for price, vol in volumes:
                if vol >= threshold:
                    self._carried_levels.append((price, session_idx))

    def _update_bar_history(self, bar: Any) -> None:
        """Keep recent bar history for absorption detection."""
        self._bar_history.append(bar)
        if len(self._bar_history) > 50:
            self._bar_history.pop(0)

    def _update_efficiency_metrics(self, bar: Any) -> None:
        """Track price movement efficiency per unit of signed flow."""
        if bar.volume == 0:
            return
        
        signed_flow = bar.delta
        if abs(signed_flow) < 0.1 * bar.volume:
            # Near-balanced bar, efficiency undefined
            return
        
        price_range = bar.high - bar.low
        if price_range == 0:
            return
        
        efficiency = price_range / abs(signed_flow)
        
        if signed_flow > 0:
            self._efficiencies_up.append(efficiency)
            if len(self._efficiencies_up) > self.efficiency_window:
                self._efficiencies_up.pop(0)
        else:
            self._efficiencies_down.append(efficiency)
            if len(self._efficiencies_down) > self.efficiency_window:
                self._efficiencies_down.pop(0)

    def _detect_absorption(self, bar: Any) -> Optional[Tuple[float, str]]:
        """
        Detect absorption: aggressive flow at a carried level that fails to move price.
        Returns (level, direction) or None.
        """
        if len(self._bar_history) < 2:
            return None
        
        prev = self._bar_history[-2]
        
        # Check if price arrived at a carried level
        for level, _ in self._carried_levels:
            level_low = level - 0.25
            level_high = level + 0.25
            
            # Did price reach this level?
            if not (bar.low <= level <= bar.high):
                continue
            
            # Is there significant flow?
            if bar.volume < 100:  # Minimum volume threshold
                continue
            
            # Check for absorption: bar makes a tail but closes back past prev extreme
            if bar.delta > self.absorption_flow_ratio * bar.volume:  # Net buy flow
                # Long absorption: high flow, but close back below prev close
                if bar.close < prev.close and bar.high > prev.high:
                    return (level, "long")
            elif bar.delta < -self.absorption_flow_ratio * bar.volume:  # Net sell flow
                # Short absorption: high flow, but close back above prev close
                if bar.close > prev.close and bar.low < prev.low:
                    return (level, "short")
        
        return None

    def _is_confirming_bar(self, bar: Any, direction: str) -> bool:
        """
        Check if bar shows efficient price movement (initiation).
        Efficiency = price range / absolute signed flow.
        """
        if bar.volume == 0:
            return False
        
        signed_flow = bar.delta
        if abs(signed_flow) < 0.1 * bar.volume:
            return False  # Near-balanced, not confirming
        
        price_range = bar.high - bar.low
        if price_range == 0:
            return False
        
        efficiency = price_range / abs(signed_flow)
        
        # Compare to recent distribution
        if direction == "long" and signed_flow > 0:
            if not self._efficiencies_up:
                return False
            avg_eff = sum(self._efficiencies_up) / len(self._efficiencies_up)
            return efficiency > avg_eff * self.efficiency_threshold
        elif direction == "short" and signed_flow < 0:
            if not self._efficiencies_down:
                return False
            avg_eff = sum(self._efficiencies_down) / len(self._efficiencies_down)
            return efficiency > avg_eff * self.efficiency_threshold
        
        return False

    def _place_entry(self, ctx: Any, level: float, direction: str, tag: str) -> None:
        """Place entry order with structural stop."""
        if len(self._bar_history) < 1:
            return
        
        absorption_bar = self._bar_history[-1]
        
        # Stop is just beyond the absorption bar's extreme
        if direction == "long":
            stop_price = absorption_bar.low - 0.25
            stop_distance = ctx.last_price - stop_price
        else:
            stop_price = absorption_bar.high + 0.25
            stop_distance = stop_price - ctx.last_price
        
        # Decline if stop is too wide
        if stop_distance > self.max_stop_distance:
            return
        
        target_distance = stop_distance * self.target_risk_multiple
        
        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=tag,
            )
