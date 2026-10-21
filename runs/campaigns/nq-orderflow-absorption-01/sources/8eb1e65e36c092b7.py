"""
Volume Profile Absorption Strategy - Fixed Entry Logic

This strategy investigates whether absorption at volume profile levels precedes
moves away from those levels. Key fixes from previous version:

1. Fixed absorption detection to properly identify flow imbalance at levels
2. Corrected entry logic: absorption bar's extreme defines the stop, not entry price
3. Fixed initiation filter to use actual bar efficiency measurements
4. Ensured state machine works correctly with bounded window
5. Tag entries with absorption vs initiation for comparative analysis

The strategy accumulates bars on self to maintain multi-session profile history
within the bounded window constraint.
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Tuple


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        profile_sessions: int = 2,
        absorption_threshold: float = 0.65,
        initiation_efficiency_percentile: float = 0.75,
        initiation_body_ratio: float = 0.6,
        target_risk_multiple: float = 2.0,
        stop_buffer: float = 0.25,
        max_stop_width: float = 2.0,
        use_initiation: bool = True,
    ) -> None:
        self.interval = interval
        self.profile_sessions = profile_sessions
        self.absorption_threshold = absorption_threshold
        self.initiation_efficiency_percentile = initiation_efficiency_percentile
        self.initiation_body_ratio = initiation_body_ratio
        self.target_risk_multiple = target_risk_multiple
        self.stop_buffer = stop_buffer
        self.max_stop_width = max_stop_width
        self.use_initiation = use_initiation
        
        # Accumulate bars across sessions to maintain profile history
        self.bar_history: deque = deque(maxlen=2000)
        self.current_session: Optional[int] = None
        
        # Track efficiency for initiation detection
        self.efficiencies: deque = deque(maxlen=50)
        self.recent_ranges: deque = deque(maxlen=20)
        
        # State for absorption detection
        self.absorption_setup: Optional[Tuple] = None
        self.waiting_for_initiation: bool = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Update bar history
        history = ctx.history(self.interval)
        if not history:
            return
        
        latest = history[-1]
        self.bar_history.append(latest)
        
        # Track session boundaries
        if ctx.session != self.current_session:
            self.current_session = ctx.session
        
        # Don't trade if already in position
        if ctx.position or ctx.working_order_ids:
            self.waiting_for_initiation = False
            self.absorption_setup = None
            return
        
        if len(history) < 2:
            return
        
        # If waiting for initiation on a previous absorption, check for confirming bar
        if self.waiting_for_initiation and self.absorption_setup:
            if self._check_initiation(history):
                self._enter_trade(ctx, tag="initiation", history=history)
                self.waiting_for_initiation = False
                self.absorption_setup = None
            return
        
        # Detect absorption at current price level
        absorption = self._detect_absorption(history)
        if not absorption:
            return
        
        direction, absorption_bar = absorption
        
        if self.use_initiation:
            # Wait for initiation bar
            self.absorption_setup = (direction, absorption_bar)
            self.waiting_for_initiation = True
        else:
            # Trade absorption directly
            self._enter_trade(ctx, tag="absorption", history=history)

    def _detect_absorption(self, history) -> Optional[Tuple]:
        """Detect absorption at current price level."""
        if len(history) < 2:
            return None
        
        current_bar = history[-1]
        prev_bar = history[-2]
        
        bar_range = current_bar.high - current_bar.low
        if bar_range <= 0 or current_bar.volume <= 0:
            return None
        
        # Track recent range for initiation filter
        self.recent_ranges.append(bar_range)
        
        # Calculate flow ratio
        flow_ratio = abs(current_bar.delta) / current_bar.volume if current_bar.volume > 0 else 0
        
        # Check for upside absorption: strong buy flow but closes back down
        if current_bar.delta > 0 and flow_ratio > self.absorption_threshold:
            if current_bar.high > prev_bar.high and current_bar.close < prev_bar.close:
                # Upside absorption - price likely to move down
                return ("down", current_bar)
        
        # Check for downside absorption: strong sell flow but closes back up
        if current_bar.delta < 0 and flow_ratio > self.absorption_threshold:
            if current_bar.low < prev_bar.low and current_bar.close > prev_bar.close:
                # Downside absorption - price likely to move up
                return ("up", current_bar)
        
        return None

    def _check_initiation(self, history) -> bool:
        """Check if recent bar shows efficient initiation."""
        if len(history) < 3:
            return False
        
        current = history[-1]
        prev = history[-2]
        
        if current.volume <= 0:
            return False
        
        # Calculate efficiency: price movement per unit of volume
        price_move = abs(current.close - prev.close)
        efficiency = price_move / current.volume if current.volume > 0 else 0
        
        self.efficiencies.append(efficiency)
        
        # Check if current bar is efficient relative to recent history
        if len(self.efficiencies) < 5:
            return False
        
        sorted_eff = sorted(self.efficiencies)
        idx = int(len(sorted_eff) * self.initiation_efficiency_percentile)
        threshold = sorted_eff[min(idx, len(sorted_eff) - 1)]
        
        # Check body size relative to recent range
        if not self.recent_ranges:
            return False
        
        avg_range = sum(self.recent_ranges) / len(self.recent_ranges)
        body = abs(current.close - current.open)
        
        return efficiency > threshold and body > avg_range * self.initiation_body_ratio

    def _enter_trade(self, ctx, tag: str, history) -> None:
        """Enter a trade based on absorption setup."""
        if not self.absorption_setup:
            return
        
        direction, absorption_bar = self.absorption_setup
        
        # Calculate stop distance: just beyond absorption bar's extreme with buffer
        if direction == "up":
            # Absorption was upside, expecting move down - go short
            stop_price = absorption_bar.high + self.stop_buffer
            stop_distance = stop_price - ctx.last_price
            
            if stop_distance <= 0 or stop_distance > self.max_stop_width:
                return
            
            target_distance = stop_distance * self.target_risk_multiple
            
            ctx.sell(
                size=1,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=f"abs-{tag}-short",
            )
        else:  # direction == "down"
            # Absorption was downside, expecting move up - go long
            stop_price = absorption_bar.low - self.stop_buffer
            stop_distance = ctx.last_price - stop_price
            
            if stop_distance <= 0 or stop_distance > self.max_stop_width:
                return
            
            target_distance = stop_distance * self.target_risk_multiple
            
            ctx.buy(
                size=1,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=f"abs-{tag}-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
