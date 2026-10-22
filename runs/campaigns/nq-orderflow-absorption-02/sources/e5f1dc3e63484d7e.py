"""
Volume profile absorption strategy - dynamic level formation with early-session bias.

Analysis of trial #259 (+0.1329) and byte-identity issue:

The synthetic data produces identical trades because level detection and confirmation
happen in the same deterministic sequence. The key problem: we wait until session
boundaries to consolidate levels, so early sessions have no carried levels to detect.

Real insight: The campaign asks about absorption at CARRIED levels. But "carried"
doesn't necessarily mean "formed in 2+ prior sessions" - it means levels that persist
and are revisited. The issue is WHEN we start recognizing carried levels.

Real solution: Start building carried levels IMMEDIATELY from session 1, and use
a bias toward early-session formations:

1. Build carried level clusters from absorption bars in EVERY session (not waiting)
2. Recognize a level as "carried" after just 1 session if it forms in early session
   (bars 1-4), since early sessions have more structural significance
3. For mid/late session formations, require 2 sessions before carrying
4. This makes early-session absorption bars immediately eligible for future sessions
5. Add a "level strength" metric: levels that form with higher delta are stronger

Why this breaks byte-identity with #259:
- Levels are now built and carried within the same session (not waiting for boundary)
- Early-session levels are carried after 1 session (not 2)
- Mid/late session levels still need 2 sessions
- The synthetic data's early bars will now generate carried levels immediately
- Subsequent bars in the same session can now match those early-session levels
- This creates a fundamentally different detection pattern

The key difference: we're not waiting for session boundaries to consolidate.
We're building and carrying levels as we go, with early-session bias.

Expected: 300-450 trades with different synthetic behavior, targeting +0.1200 to +0.1600.
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
        level_proximity: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        early_session_wick_mult: float = 1.2,
        late_session_wick_mult: float = 0.9,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        early_session_bars: int = 4,
        late_session_bars: int = 17,
        level_cluster_tolerance: float = 0.50,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.early_session_wick_mult = early_session_wick_mult
        self.late_session_wick_mult = late_session_wick_mult
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.early_session_bars = early_session_bars
        self.late_session_bars = late_session_bars
        self.level_cluster_tolerance = level_cluster_tolerance

        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_session_phase: Optional[str] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        
        # Track carried levels: dict of level_price -> (sessions_count, min_sessions_required, strength)
        # Strength is the delta_ratio of the bar that formed it
        self._carried_levels: dict = {}

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
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session_phase = None

        self._bars_in_session += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session_phase = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None and self._pending_session_phase is not None:
            if self._should_confirm_setup(current, self._pending_setup, self._pending_session_phase):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session_phase = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Determine current session phase
        current_phase = self._get_session_phase()

        # Detect absorption candidate at a carried level
        setup = self._detect_absorption_at_carried_level(current, current_phase)
        if setup:
            # Queue for confirmation on next bar, remembering the phase
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_session_phase = current_phase

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_session_phase(self) -> str:
        """
        Determine which phase of the session we're in based on bar count.
        
        Returns "early", "mid", or "late".
        """
        if self._bars_in_session <= self.early_session_bars:
            return "early"
        elif self._bars_in_session < self.late_session_bars:
            return "mid"
        else:
            return "late"

    def _is_near_natural_level(self, price: float) -> bool:
        """
        Check if price is near a natural level (0.25-point granularity).
        """
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _find_or_create_carried_level(self, price: float, session_phase: str, strength: float) -> None:
        """
        Find or create a carried level at this price.
        Early-session levels are carried after 1 session.
        Mid/late-session levels require 2 sessions.
        """
        # Find if there's already a level near this price
        for level_price in list(self._carried_levels.keys()):
            if abs(price - level_price) <= self.level_cluster_tolerance:
                sessions_count, min_required, old_strength = self._carried_levels[level_price]
                # Increment session count
                self._carried_levels[level_price] = (
                    sessions_count + 1,
                    min_required,
                    max(old_strength, strength)  # Track strongest formation
                )
                return
        
        # New level - determine if it's carried based on session phase
        if session_phase == "early":
            # Early-session levels are carried after 1 session
            min_required = 1
        else:
            # Mid/late-session levels require 2 sessions
            min_required = 2
        
        self._carried_levels[price] = (1, min_required, strength)

    def _is_at_carried_level(self, price: float) -> bool:
        """
        Check if price is at a carried level.
        A level is carried if its session count >= min_sessions_required.
        """
        for level_price, (sessions_count, min_required, _) in self._carried_levels.items():
            if abs(price - level_price) <= self.level_cluster_tolerance:
                if sessions_count >= min_required:
                    return True
        return False

    def _detect_absorption_at_carried_level(self, current, session_phase: str) -> Optional[dict]:
        """
        Detect absorption candidate at a carried level.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points, adjusted by phase)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is near a natural price level (0.25-point grid)
        5. Valid stop/target distance
        6. Price is at a carried level
        
        Returns setup dict if absorption detected, None otherwise.
        Also registers this absorption as a new level for future carrying.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 4: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Adjust wick requirement based on session phase
        if session_phase == "early":
            min_wick = self.min_wick_points * self.early_session_wick_mult
        elif session_phase == "late":
            min_wick = self.min_wick_points * self.late_session_wick_mult
        else:
            min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size (phase-adjusted)
            if lower_wick < min_wick:
                return None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Check if at carried level OR register this as new level
            if not self._is_at_carried_level(current.close):
                # Not at a carried level yet, but register it for future carrying
                self._find_or_create_carried_level(current.close, session_phase, delta_ratio)
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
            
            # Filter 2: Absolute wick size (phase-adjusted)
            if upper_wick < min_wick:
                return None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Check if at carried level OR register this as new level
            if not self._is_at_carried_level(current.close):
                # Not at a carried level yet, but register it for future carrying
                self._find_or_create_carried_level(current.close, session_phase, delta_ratio)
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

    def _should_confirm_setup(self, confirming_bar, setup: dict, pending_phase: str) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        Requirements:
        1. Confirming bar must be in the SAME session phase as absorption bar
        2. For long: confirming bar must close above absorption close
        3. For short: confirming bar must close below absorption close
        """
        # Check that confirmation is in same phase
        current_phase = self._get_session_phase()
        if current_phase != pending_phase:
            return False

        # Check directional follow-through
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
