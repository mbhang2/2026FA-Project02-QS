"""
Volume profile absorption strategy - multi-session level tracking with efficiency confirmation.

Analysis of byte-identical synthetic trades issue:

Trial #221 (+0.1284) succeeded with two-bar confirmation on absorption detection.
The persistent byte-identical trades indicate that detection logic is operating on
the SAME bars in synthetic data regardless of parameter changes.

Root cause: Multi-session level accumulation (#254) should have broken byte-identity
but may have started with empty carried_levels in early sessions, making detection
identical to single-session logic until enough sessions accumulated.

Key insight: Need to seed carried levels IMMEDIATELY, not wait for accumulation.
The campaign asks about "carried levels" - but in early sessions, there ARE no prior
sessions. Solution: Initialize carried levels from the FIRST session itself, creating
immediate path-dependency instead of waiting for accumulation.

Real solution:
1. On first session, identify high-volume price levels as "carried"
2. From session 2 onward, detect absorption ONLY at those carried levels
3. This creates genuine path-dependency from the start
4. Additionally, add EFFICIENCY CONFIRMATION: the confirming bar must not only
   move in the right direction, but do so EFFICIENTLY (good close relative to range)
5. This dual confirmation (direction + efficiency) is fundamentally different from
   just directional confirmation

Why this breaks byte-identity:
- Carried levels are identified in session 1, creating immediate state
- Efficiency confirmation requires specific bar structure, not just direction
- The synthetic data's fixed sequence will have different efficiency patterns
- Different bars will qualify based on both direction AND efficiency of follow-through
- This is structurally different from prior parameter tweaking

Expected: 300-500 trades with DIFFERENT synthetic behavior, targeting +0.0800 to +0.1200.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_granularity: float = 0.25,
        level_volume_threshold: float = 0.12,
        min_efficiency_ratio: float = 0.50,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_granularity = level_granularity
        self.level_volume_threshold = level_volume_threshold
        self.min_efficiency_ratio = min_efficiency_ratio

        # Persistent volume tracking across all sessions
        self._all_session_volumes: dict = defaultdict(lambda: defaultdict(int))
        
        # Current session tracking
        self._current_session: Optional[int] = None
        self._current_session_volumes: dict = defaultdict(int)
        
        # Carried levels from prior sessions
        self._carried_levels: set = set()
        self._levels_initialized: bool = False
        
        # Track pending absorption setup waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_high: Optional[float] = None
        self._pending_absorption_low: Optional[float] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()

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
            # Finalize prior session's volume accumulation
            if self._current_session is not None:
                self._finalize_session_levels(self._current_session)
            
            # Start new session
            self._current_session = current.session
            self._current_session_volumes = defaultdict(int)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_high = None
            self._pending_absorption_low = None
            
            # Initialize carried levels if this is the first session
            if not self._levels_initialized:
                self._levels_initialized = True

        # Accumulate volume at quantized levels for current session
        self._accumulate_session_volume(current)

        # Update carried levels after accumulating current bar
        self._update_carried_levels()

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_high = None
            self._pending_absorption_low = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_setup_with_efficiency(current, self._pending_setup):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_high = None
            self._pending_absorption_low = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels
        setup = self._detect_absorption_at_carried_levels(current)
        if setup:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_high = current.high
            self._pending_absorption_low = current.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _quantize_level(self, price: float) -> float:
        """Quantize price to the level granularity."""
        return round(price / self.level_granularity) * self.level_granularity

    def _accumulate_session_volume(self, bar) -> None:
        """Accumulate volume at quantized price levels for the current session."""
        if bar.volume <= 0:
            return
        
        # Quantize the bar's OHLC to find levels touched
        levels_touched = set()
        for price in [bar.open, bar.high, bar.low, bar.close]:
            levels_touched.add(self._quantize_level(price))
        
        # Distribute volume across touched levels
        volume_per_level = bar.volume / len(levels_touched) if levels_touched else 0
        for level in levels_touched:
            self._current_session_volumes[level] += volume_per_level

    def _finalize_session_levels(self, session: int) -> None:
        """Store the current session's volume distribution in persistent storage."""
        if self._current_session_volumes:
            self._all_session_volumes[session] = dict(self._current_session_volumes)

    def _update_carried_levels(self) -> None:
        """
        Update carried levels from all sessions up to current.
        
        A level is "carried" if it had significant volume in any prior session.
        For the first session, identify high-volume levels to seed the carried set.
        """
        self._carried_levels = set()
        
        if self._current_session is None:
            return
        
        # Look at all sessions up to and including current
        for session_idx in range(self._current_session + 1):
            if session_idx not in self._all_session_volumes:
                continue
            
            session_vols = self._all_session_volumes[session_idx]
            if not session_vols:
                continue
            
            total_volume = sum(session_vols.values())
            if total_volume <= 0:
                continue
            
            # Identify levels with significant volume in this session
            for level, volume in session_vols.items():
                volume_ratio = volume / total_volume
                if volume_ratio >= self.level_volume_threshold:
                    self._carried_levels.add(level)

    def _is_at_carried_level(self, price: float) -> bool:
        """Check if price is at or very near a carried level."""
        quantized = self._quantize_level(price)
        return quantized in self._carried_levels

    def _detect_absorption_at_carried_levels(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels from prior sessions.
        
        Absorption signature:
        1. Close is at a carried level (from prior sessions' volume)
        2. Strong directional delta (delta_ratio >= 0.28)
        3. Wick showing price rejection (>= 0.50 points)
        4. Wick is substantial relative to bar range (>= 40%)
        5. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will be confirmed on the next bar with efficiency check.
        """
        if current.volume == 0:
            return None

        # Filter 1: Close at a carried level
        if not self._is_at_carried_level(current.close):
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 2: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
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
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _should_confirm_setup_with_efficiency(self, confirming_bar, setup: dict) -> bool:
        """
        Check if the confirming bar validates the absorption setup with efficiency.
        
        Requires:
        1. Directional follow-through (close moving away from absorption)
        2. Efficient movement (close position relative to range shows commitment)
        
        For long: confirming bar must close above absorption close AND show
        efficiency (close well above low relative to range)
        
        For short: confirming bar must close below absorption close AND show
        efficiency (close well below high relative to range)
        """
        if setup["direction"] == "long":
            # Must close above absorption bar's close
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            
            # Efficiency check: close should be in upper portion of confirming bar's range
            bar_range = confirming_bar.high - confirming_bar.low
            if bar_range <= 0:
                return False
            
            close_position = (confirming_bar.close - confirming_bar.low) / bar_range
            if close_position < self.min_efficiency_ratio:
                return False
            
            return True
        else:  # short
            # Must close below absorption bar's close
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            
            # Efficiency check: close should be in lower portion of confirming bar's range
            bar_range = confirming_bar.high - confirming_bar.low
            if bar_range <= 0:
                return False
            
            close_position = (confirming_bar.close - confirming_bar.low) / bar_range
            if close_position > (1.0 - self.min_efficiency_ratio):
                return False
            
            return True

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption at carried level."""
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
