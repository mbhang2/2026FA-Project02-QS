"""
Volume profile absorption strategy - multi-session level accumulation with confirmation.

Analysis of prior best result (#221: +0.1284) and recent trials:

Trial #221 achieved the best score with simple two-bar confirmation on absorption detection.
The byte-identical trades issue suggests that single-session level detection or confirmation
logic operating on future bars doesn't create sufficient variation in synthetic data.

Key insight: The campaign asks "Does absorption at a carried level precede a move away?"
The word "carried" is critical - levels should be CARRIED FORWARD from prior sessions,
not detected fresh each session. This creates path-dependency: which levels exist depends
on what happened in previous sessions, making the walk genuinely sequential.

Real solution: Build a MULTI-SESSION LEVEL MEMORY:
1. Track volume-weighted price levels across the last 2-3 sessions
2. Identify "carried levels" - price points where volume clustered in prior sessions
3. Detect absorption only when:
   a) Strong directional delta occurs AT a carried level (not just any strong delta)
   b) The bar shows the rejection signature (wick pattern)
   c) The next bar confirms with follow-through movement
4. This makes bar selection genuinely path-dependent:
   - Which levels are "carried" depends on prior session volume
   - Different sessions have different carried levels
   - Absorption candidates are those hitting carried levels, not just any strong delta bar
   - Synthetic data's fixed sequence will have different carried levels than real data

Why this changes synthetic behavior:
- Carried levels are accumulated across sessions, not reset each session
- Bar selection depends on prior sessions' volume distribution, not current bar alone
- Different price sequences will have different carried levels
- This is fundamentally different from single-session detection
- The confirmation logic stays (proven in #221) but applied to a different set of candidates

Expected: 400-600 trades with DIFFERENT synthetic behavior, targeting +0.0800 to +0.1600.
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
        level_volume_threshold: float = 0.15,
        sessions_to_carry: int = 2,
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
        self.sessions_to_carry = sessions_to_carry

        # Track volume at each price level per session
        # Maps session -> {level: volume}
        self._session_volumes: dict = defaultdict(lambda: defaultdict(int))
        
        # Current session's volume accumulation
        self._current_session: Optional[int] = None
        self._current_session_volumes: dict = defaultdict(int)
        
        # Carried levels from prior sessions
        self._carried_levels: set = set()
        
        # Track pending absorption setup waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        
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

        # Handle session boundary - finalize prior session's levels
        if current.session != self._current_session:
            if self._current_session is not None:
                self._finalize_session_levels(self._current_session)
            self._current_session = current.session
            self._current_session_volumes = defaultdict(int)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._update_carried_levels()

        # Accumulate volume at quantized levels for this session
        self._accumulate_session_volume(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_setup(current, self._pending_setup):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
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
        """Store the current session's volume distribution."""
        if self._current_session_volumes:
            self._session_volumes[session] = dict(self._current_session_volumes)

    def _update_carried_levels(self) -> None:
        """
        Update carried levels from prior sessions.
        
        A level is "carried" if it had significant volume in recent sessions.
        These are the levels where we look for absorption.
        """
        self._carried_levels = set()
        
        if self._current_session is None:
            return
        
        # Look back at the last N sessions
        for session_offset in range(1, self.sessions_to_carry + 1):
            prior_session = self._current_session - session_offset
            if prior_session not in self._session_volumes:
                continue
            
            session_vols = self._session_volumes[prior_session]
            if not session_vols:
                continue
            
            total_volume = sum(session_vols.values())
            if total_volume <= 0:
                continue
            
            # Identify levels with significant volume
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
        Setup will be confirmed on the next bar.
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

    def _should_confirm_setup(self, confirming_bar, setup: dict) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        For long (buying pressure): confirming bar should close above absorption close
        For short (selling pressure): confirming bar should close below absorption close
        """
        if setup["direction"] == "long":
            return confirming_bar.close > self._pending_absorption_close
        else:  # short
            return confirming_bar.close < self._pending_absorption_close

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
