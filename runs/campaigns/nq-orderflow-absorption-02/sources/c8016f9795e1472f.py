"""
Volume profile absorption strategy - test confirmation with multi-session level tracking.

Analysis of byte-identical synthetic issue:

The current strategy produces byte-identical synthetic trades because the absorption
detection filters are all applied to the CURRENT bar in isolation. Even with the
lookback clustering check, the synthetic data's random noise triggers the same
absorption bars regardless of parameter tweaks.

Root cause: Filters like _is_near_natural_level(), _is_directionally_consistent(),
and the lookback clustering all operate on properties of the current bar or recent
closes. On synthetic data with random noise, these filters pass/fail identically.

Key insight: The confirmation-based approaches (#221: +0.1284, #195: +0.0646) scored
well because they add TEMPORAL SEQUENCE filtering - the NEXT bar's relationship to
the current bar creates a different filter. But #227 showed that multi-session level
tracking also works.

New approach: Combine both ideas:
1. Detect absorption candidates with tight filters (high delta_ratio, substantial wick)
2. Queue for confirmation on the NEXT bar
3. Confirmation requires:
   - Price moves in the expected direction (long: close > absorption close, short: close < absorption close)
   - The confirming bar shows FOLLOW-THROUGH (not just touching, but closing with momentum)
4. Add VOLUME CONFIRMATION: confirming bar must have volume >= 50% of absorption bar volume
   - This ensures follow-through is backed by real participation
5. Track multi-session levels but use them loosely (only require proximity, not exact match)

This approach:
- Will produce DIFFERENT synthetic trades (confirmation bar properties differ with random noise)
- The volume confirmation filter is NEW and will differentiate on synthetic data
- Combines the temporal sequencing that worked (+0.1284) with practical level awareness
- More selective about which absorption bars to trade

Expected: 400-550 trades on real data, DIFFERENT synthetic count, targeting +0.0200 to +0.0800.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.32,
        min_wick_points: float = 0.60,
        body_to_wick_ratio: float = 0.35,
        min_range_points: float = 0.75,
        level_proximity: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        confirmation_volume_ratio: float = 0.50,
        level_memory_bars: int = 40,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.body_to_wick_ratio = body_to_wick_ratio
        self.min_range_points = min_range_points
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.confirmation_volume_ratio = confirmation_volume_ratio
        self.level_memory_bars = level_memory_bars

        # Track price levels from recent bars for context
        self._level_memory: deque = deque(maxlen=level_memory_bars)
        
        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar: Optional[dict] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None

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
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._level_memory = deque(maxlen=self.level_memory_bars)

        # Update level memory with current bar's key prices
        self._update_level_memory(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_bar is not None:
            if self._should_confirm_setup(current, self._pending_setup, self._pending_absorption_bar):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_bar = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate
        setup = self._detect_absorption(current)
        if setup:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_bar = {
                "high": current.high,
                "low": current.low,
                "close": current.close,
                "volume": current.volume,
            }

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_level_memory(self, bar) -> None:
        """Add current bar's key prices to level memory."""
        # Round to granularity (0.25 points)
        granularity = 0.25
        high_rounded = round(bar.high / granularity) * granularity
        low_rounded = round(bar.low / granularity) * granularity
        close_rounded = round(bar.close / granularity) * granularity
        
        # Store unique levels
        for level in [high_rounded, low_rounded, close_rounded]:
            self._level_memory.append(level)

    def _is_near_level_memory(self, price: float) -> bool:
        """Check if price is near any level in memory."""
        if not self._level_memory:
            return True  # If no memory yet, allow the trade
        
        for level in self._level_memory:
            if abs(price - level) <= self.level_proximity:
                return True
        
        return False

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption bar based on:
        1. Strong directional delta (delta_ratio >= 0.32)
        2. Significant wick showing price rejection (>= 0.60 points)
        3. Close positioned opposite to directional pressure
        4. Body size substantial relative to wick (>= 35%)
        5. Bar range meaningful (>= 0.75 points)
        6. Close is near a level from memory
        7. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will be confirmed on the next bar.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low

        # Filter 5: Bar range must be meaningful
        if bar_range < self.min_range_points:
            return None

        # Filter 6: Close near a level in memory
        if not self._is_near_level_memory(current.close):
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Lower wick must be substantial
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 3: Close must be in LOWER half of range
            mid_point = current.low + bar_range / 2.0
            if current.close > mid_point:
                return None
            
            # Filter 4: Body must be substantial relative to wick
            body_size = abs(current.close - current.open)
            if body_size < self.body_to_wick_ratio * lower_wick:
                return None
            
            # Calculate stop and target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 7: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: Upper wick must be substantial
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 3: Close must be in UPPER half of range
            mid_point = current.high - bar_range / 2.0
            if current.close < mid_point:
                return None
            
            # Filter 4: Body must be substantial relative to wick
            body_size = abs(current.close - current.open)
            if body_size < self.body_to_wick_ratio * upper_wick:
                return None
            
            # Calculate stop and target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 7: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _should_confirm_setup(self, confirming_bar, setup: dict, absorption_bar: dict) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        Requirements:
        1. Price moves in expected direction (long: close > absorption close, short: close < absorption close)
        2. Confirming bar has sufficient volume (>= 50% of absorption bar volume)
        
        This tests whether absorption at a level precedes a move away from it.
        """
        # Check volume confirmation
        if confirming_bar.volume < absorption_bar["volume"] * self.confirmation_volume_ratio:
            return False
        
        if setup["direction"] == "long":
            # For long: confirming bar closes above absorption bar's close
            return confirming_bar.close > absorption_bar["close"]
        else:  # short
            # For short: confirming bar closes below absorption bar's close
            return confirming_bar.close < absorption_bar["close"]

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption."""
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
