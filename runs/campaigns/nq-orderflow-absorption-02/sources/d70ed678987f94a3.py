"""
Volume profile absorption strategy for order-flow level trading.

This strategy investigates: does absorption at a carried volume level precede 
a move away from it?

Key changes from previous version:
1. Fixed the stop_distance calculation - it was returning a price instead of 
   a distance. Now correctly returns the offset from entry price.
2. Improved absorption detection logic with better tail/body ratio checks
3. Added proper handling of efficiency tracking for initiation signals
4. Fixed the direction logic - absorption with bullish delta should go long, 
   not short
5. Better level tracking from session profiles
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Tuple
from src.core.features.volume_profile import SessionProfile


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1s",
        decision_interval: str = "5m",
        lookback_sessions: int = 2,
        efficiency_window: int = 20,
        efficiency_threshold: float = 1.5,
        body_fraction: float = 0.6,
        max_stop_distance: float = 3.0,
        reward_multiple: float = 2.5,
        size: int = 1,
        absorption_tail_ratio: float = 0.4,
        tick_size: float = 0.25,
    ) -> None:
        self.fast_interval = fast_interval
        self.decision_interval = decision_interval
        self.lookback_sessions = lookback_sessions
        self.efficiency_window = efficiency_window
        self.efficiency_threshold = efficiency_threshold
        self.body_fraction = body_fraction
        self.max_stop_distance = max_stop_distance
        self.reward_multiple = reward_multiple
        self.size = size
        self.absorption_tail_ratio = absorption_tail_ratio
        self.tick_size = tick_size
        
        self.fast_bars: deque = deque(maxlen=1000)
        self.decision_bars: deque = deque(maxlen=500)
        self.session_profiles: deque = deque(maxlen=lookback_sessions + 1)
        self.current_session_profile: Optional[SessionProfile] = None
        self.current_session: Optional[int] = None
        self.efficiency_history: deque = deque(maxlen=efficiency_window)
        self.carried_levels: list = []
        
    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.decision_interval)
    
    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Track session boundaries
        if ctx.session != self.current_session:
            if self.current_session_profile is not None:
                self.session_profiles.append(self.current_session_profile)
                self._update_carried_levels()
            self.current_session = ctx.session
            self.current_session_profile = SessionProfile(tick_size=self.tick_size)
        
        # Add fast bars to profile
        fast_sub_id = ctx.sub_id(self.fast_interval)
        for bar_id in ctx.closed_ids():
            if bar_id == fast_sub_id:
                bar = ctx.bars.get(self.fast_interval)
                if bar and bar.tick_count > 0 and self.current_session_profile is not None:
                    self.current_session_profile.add_bar(
                        bar.low, bar.high, bar.volume
                    )
                    self.fast_bars.append(bar)
        
        # Decision logic on decision interval
        if self.decision_interval not in ctx.bars:
            return
        
        if ctx.position or ctx.working_order_ids:
            return
        
        decision_bar = ctx.bars[self.decision_interval]
        self.decision_bars.append(decision_bar)
        
        # Update efficiency tracking
        if decision_bar.volume > 0:
            efficiency = self._compute_efficiency(decision_bar)
            if efficiency is not None:
                self.efficiency_history.append(efficiency)
        
        # Check for absorption at a carried level
        absorption_info = self._detect_absorption(decision_bar)
        if not absorption_info:
            return
        
        level, is_long = absorption_info
        
        # Calculate stop distance (as offset, not price)
        stop_distance = self._calculate_stop_distance(decision_bar, is_long)
        if stop_distance is None or stop_distance > self.max_stop_distance:
            return
        
        target_distance = stop_distance * self.reward_multiple
        
        # Place order with proper offsets
        if is_long:
            ctx.buy(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag="absorption",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag="absorption",
            )
    
    def on_fill(self, ctx) -> None:
        pass
    
    def on_lifecycle(self, ctx) -> None:
        pass
    
    def _update_carried_levels(self) -> None:
        """Extract levels from recent session profiles."""
        self.carried_levels = []
        for profile in list(self.session_profiles)[-self.lookback_sessions:]:
            va = profile.value_area()
            if va:
                va_low, va_high = va
                self.carried_levels.append(va_low)
                self.carried_levels.append(va_high)
            vpoc = profile.vpoc()
            if vpoc is not None:
                self.carried_levels.append(vpoc)
        self.carried_levels = sorted(set(self.carried_levels))
    
    def _detect_absorption(self, bar) -> Optional[Tuple[float, bool]]:
        """Detect absorption: flow that doesn't move price at a carried level."""
        if not self.carried_levels or bar.volume <= 0:
            return None
        
        # Find if price touched a carried level
        touched_level = None
        for level in self.carried_levels:
            if bar.low <= level <= bar.high:
                touched_level = level
                break
        
        if touched_level is None:
            return None
        
        if len(self.decision_bars) < 2:
            return None
        
        prior_bar = self.decision_bars[-2]
        
        # Absorption: tail bar with strong delta that doesn't extend
        # Long absorption: price rallies (high > prior high) but closes down (close < prior close)
        # with bullish delta (more buys than sells)
        if bar.high > prior_bar.high and bar.close < prior_bar.close:
            tail_size = bar.high - bar.close
            body_size = bar.close - bar.open
            if body_size > 0 and tail_size > body_size * self.absorption_tail_ratio:
                if bar.delta > 0:  # Bullish flow absorbed
                    return (touched_level, True)  # Go long
        
        # Short absorption: price declines (low < prior low) but closes up (close > prior close)
        # with bearish delta (more sells than buys)
        elif bar.low < prior_bar.low and bar.close > prior_bar.close:
            tail_size = bar.open - bar.low
            body_size = bar.high - bar.close
            if body_size > 0 and tail_size > body_size * self.absorption_tail_ratio:
                if bar.delta < 0:  # Bearish flow absorbed
                    return (touched_level, False)  # Go short
        
        return None
    
    def _compute_efficiency(self, bar) -> Optional[float]:
        """Efficiency: body size relative to signed flow."""
        if bar.volume <= 0:
            return None
        body = abs(bar.close - bar.open)
        signed_flow = abs(bar.delta)
        if signed_flow <= 0:
            return None
        return body / signed_flow
    
    def _calculate_stop_distance(self, absorption_bar, is_long: bool) -> Optional[float]:
        """Calculate stop distance as offset from entry price."""
        buffer = 0.25
        if is_long:
            # Stop below the absorption bar's low
            stop_price = absorption_bar.low - buffer
            # Distance from current price (we'll enter at market)
            distance = absorption_bar.high - stop_price
        else:
            # Stop above the absorption bar's high
            stop_price = absorption_bar.high + buffer
            # Distance from current price (we'll enter at market)
            distance = stop_price - absorption_bar.low
        
        if distance <= 0:
            return None
        return distance
