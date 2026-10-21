"""
Volume profile absorption strategy - refined focus on signal detection and execution.

Key insights from prior attempts:

1. Previous trials (#0, #1, #3) scored zero or near-zero because they weren't 
   actually trading the signal - they had the logic but it wasn't triggering.
   
2. Trial #3 had absorption detection logic but was overly complex (1s bars, 
   confirmation logic, efficiency tracking). Trial after it simplified but still 
   scored zero - likely the absorption signal was too rare or too strict.

3. The core issue: absorption detection requires comparing bar structure (close 
   vs open direction) against delta direction. This is rare. Need to:
   - Loosen absorption criteria slightly while keeping the core idea
   - Ensure the signal actually fires on real data
   - Focus on the simplest version that trades

4. Key changes from #3:
   - Use 5m bars only (already have delta, no budget issues)
   - Simpler level tracking: just track highest-volume price (POC) per session
   - Loosen absorption: strong delta in one direction + bar that doesn't extend 
     far in that direction (not necessarily closing opposite)
   - Structural stop at bar extreme + buffer
   - Target as multiple of stop to reach next level
   - Keep state on self for bounded history window

5. The absorption logic:
   - Bar arrives at a carried level (POC from current or prior session)
   - Bar has strong one-way delta (>55% of volume in one direction)
   - Bar fails to extend significantly in the delta direction (high/low range 
     is small relative to delta magnitude)
   - This suggests absorption: aggressive flow arrived but couldn't move price
   - Trade the opposite direction expecting reversion/follow-through

6. Risk management:
   - Structural stop just beyond the bar extreme where flow was absorbed
   - Decline setups with stops wider than max_stop_width
   - Hold for moves between levels (multi-bar holds)
"""

from __future__ import annotations
from typing import Dict, Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        sessions_to_carry: int = 1,
        delta_threshold: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_width: float = 2.5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.sessions_to_carry = sessions_to_carry
        self.delta_threshold = delta_threshold
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_width = max_stop_width
        self.size = size

        # State accumulated across bars
        self.session_profiles: Dict[int, Dict[float, float]] = {}
        self.current_session: Optional[int] = None
        self.prev_bar: Optional[object] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes and prune old data
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            # Prune profiles older than sessions_to_carry
            sessions_to_keep = set()
            for offset in range(self.sessions_to_carry + 1):
                s = ctx.session - offset
                if s >= 0:
                    sessions_to_keep.add(s)
            self.session_profiles = {
                s: p for s, p in self.session_profiles.items()
                if s in sessions_to_keep
            }

        # Get the bar that just closed
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Update volume profile
        self._update_profile(ctx.session, bar)

        # Need previous bar for absorption detection
        if self.prev_bar is None:
            self.prev_bar = bar
            return

        # Don't trade if already have position
        if ctx.position or ctx.working_order_ids:
            self.prev_bar = bar
            return

        # Detect absorption at a carried level
        signal = self._detect_absorption(bar, ctx.session)

        if signal is not None:
            level, side, bar_extreme = signal
            stop_distance = abs(bar_extreme - level) + self.stop_buffer

            # Decline if stop is too wide
            if stop_distance > self.max_stop_width:
                self.prev_bar = bar
                return

            target_distance = stop_distance * self.target_multiple

            # Place order with structural stop
            if side == Side.BUY:
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

        self.prev_bar = bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_profile(self, session: int, bar) -> None:
        """Build volume profile from bars."""
        if bar.volume <= 0:
            return

        if session not in self.session_profiles:
            self.session_profiles[session] = {}

        # Distribute volume across price range at 0.25 increments
        tick_size = 0.25
        num_ticks = max(1, int((bar.high - bar.low) / tick_size) + 1)
        vol_per_tick = bar.volume / num_ticks

        price = bar.low
        while price <= bar.high + 0.001:
            rounded_price = round(price / tick_size) * tick_size
            self.session_profiles[session][rounded_price] = (
                self.session_profiles[session].get(rounded_price, 0.0) + vol_per_tick
            )
            price += tick_size

    def _get_carried_levels(self, current_session: int) -> Dict[float, int]:
        """Extract point of control from carried sessions."""
        levels: Dict[float, int] = {}

        for offset in range(self.sessions_to_carry + 1):
            session = current_session - offset
            if session < 0:
                continue

            profile = self.session_profiles.get(session)
            if not profile or len(profile) == 0:
                continue

            # Find the price level with highest volume (point of control)
            poc, _ = max(profile.items(), key=lambda x: x[1])
            if poc not in levels:
                levels[poc] = offset

        return levels

    def _detect_absorption(
        self, bar, session: int
    ) -> Optional[Tuple[float, Side, float]]:
        """
        Detect absorption: bar arrives at a carried level with strong one-way
        delta but the bar range is small relative to delta magnitude.
        
        This suggests aggressive flow arrived but could not move price effectively.
        Trade the opposite direction expecting the absorbed flow to reverse.

        Returns: (level_price, side, bar_extreme) or None
        """
        carried_levels = self._get_carried_levels(session)
        if not carried_levels:
            return None

        # Check if current bar contains a carried level
        level_at_bar = None
        for level in carried_levels.keys():
            if bar.low <= level <= bar.high:
                level_at_bar = level
                break

        if level_at_bar is None:
            return None

        # Check for strong one-way delta
        if bar.volume <= 0:
            return None

        delta_ratio = abs(bar.delta) / bar.volume
        if delta_ratio < self.delta_threshold:
            return None

        bar_range = bar.high - bar.low
        
        # Long absorption: strong buying pressure but small range
        # Suggests buyers came in but couldn't push price up effectively
        if bar.delta > 0:
            # Small range relative to volume suggests absorption
            # Trade short: expect the move to go the other way
            if bar_range < 0.75:  # Small range threshold
                return (level_at_bar, Side.SELL, bar.low)

        # Short absorption: strong selling pressure but small range
        # Suggests sellers came in but couldn't push price down effectively
        elif bar.delta < 0:
            # Small range relative to volume suggests absorption
            # Trade long: expect the move to go the other way
            if bar_range < 0.75:  # Small range threshold
                return (level_at_bar, Side.BUY, bar.high)

        return None
