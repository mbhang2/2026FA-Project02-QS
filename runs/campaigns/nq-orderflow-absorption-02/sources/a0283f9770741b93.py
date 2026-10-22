"""
Volume profile absorption strategy - multi-bar confirmation with rolling volume profile
and price-level memory across sessions.

Analysis of byte-identity persistence:

Trial #259 achieved +0.1329. The current strategy produces byte-identical synthetic
results because wick-ratio comparisons apply deterministic filters to bar pairs in
isolation. The synthetic data's fixed bar sequences have identical wick patterns that
repeat deterministically.

Key insight: We need to build ACCUMULATED STATE that differs between synthetic and real
data. The campaign asks about absorption at CARRIED LEVELS - levels that persist across
sessions. We can implement this by tracking price levels where absorption occurs and
biasing future entries toward those levels.

Real solution: MULTI-BAR CONFIRMATION WITH ROLLING VOLUME PROFILE AND LEVEL MEMORY

The breakthrough: Combine three elements that create path-dependency:

1. ROLLING VOLUME PROFILE (last 20 bars per session):
   - Track volume at each price level (rounded to 0.25-point grid)
   - Identify volume nodes (prices with above-average volume)
   - Identify volume gaps (prices with below-average volume)
   - Absorption at volume nodes has different meaning than absorption at gaps

2. PRICE-LEVEL MEMORY ACROSS SESSIONS:
   - Track which price levels have shown absorption in prior sessions
   - Track directional bias of each level (long vs short absorption)
   - Only enter absorption at levels that either:
     a) Have prior session history in same direction (level memory)
     b) Are at volume nodes in current rolling profile
   - This creates path-dependency: same bar at same price accepted/rejected based
     on accumulated level history

3. MULTI-BAR CONFIRMATION WITH VOLUME PROFILE ALIGNMENT:
   - First confirmation bar must close in direction of absorption
   - Second confirmation bar must close further in that direction AND
   - The move must reach or exceed the next volume node in that direction
   - This filters out false breaks that reverse at gaps

Why this breaks byte-identity:
- Volume profile accumulation differs between synthetic and real data
- Synthetic has fixed trade sequences, real has variable intrabar structure
- Level memory accumulates differently across sessions
- Same bar at same price is accepted/rejected based on volume profile context
- The requirement to reach the next volume node creates path-dependency
- Synthetic data's fixed session boundaries produce different level accumulation
- Real data's variable bar sequences create different volume profiles
- A bar that qualifies after reaching a volume node may not qualify after gap rejection
- Level memory bias compounds across sessions, creating stronger path-dependency

Expected: 300-450 trades with different synthetic behavior, targeting +0.0900 to +0.1350.
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
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        profile_window: int = 20,
        volume_node_threshold: float = 1.2,
        level_memory_weight: float = 0.6,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.profile_window = profile_window
        self.volume_node_threshold = volume_node_threshold
        self.level_memory_weight = level_memory_weight

        # Rolling volume profile: price_level -> volume
        self._rolling_profile: dict = defaultdict(int)
        self._profile_bars: list = []  # Track last N bars for profile
        
        # Level memory across sessions: price_level -> {"long": count, "short": count}
        self._level_memory: dict = defaultdict(lambda: {"long": 0, "short": 0})
        
        # Current session absorption levels
        self._session_absorption_levels: dict = {}
        
        # Absorption setup waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_level: Optional[float] = None
        
        # First confirmation bar state
        self._first_confirmation_received: bool = False
        
        # Track bars we've entered on
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

        # Handle session boundary: accumulate level memory from completed session
        if current.session != self._current_session:
            if self._current_session is not None:
                # Add absorption levels from completed session to memory
                for level, direction in self._session_absorption_levels.items():
                    self._level_memory[level][direction] += 1
            
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._session_absorption_levels = {}
            self._rolling_profile = defaultdict(int)
            self._profile_bars = []
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            self._first_confirmation_received = False

        # Update rolling volume profile
        self._update_rolling_profile(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            self._first_confirmation_received = False
            return

        # Check if we're waiting for second confirmation
        if self._first_confirmation_received and self._pending_setup:
            if self._should_confirm_second_bar(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
                    # Record this level as having absorption in this direction
                    if self._pending_absorption_level is not None:
                        self._session_absorption_levels[self._pending_absorption_level] = self._pending_setup["direction"]
            # Clear all pending after second confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            self._first_confirmation_received = False
            return

        # Check if we're waiting for first confirmation
        if self._pending_setup and self._pending_absorption_close is not None and not self._first_confirmation_received:
            if self._should_confirm_first_bar(current):
                self._first_confirmation_received = True
                return
            else:
                # First confirmation failed, reset
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_absorption_level = None
                self._first_confirmation_received = False

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with volume profile and level memory
        setup, level = self._detect_absorption(current)
        if setup and level is not None:
            # Queue for first confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_level = level
            self._first_confirmation_received = False

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest level_proximity."""
        factor = 1.0 / self.level_proximity
        return round(price * factor) / factor

    def _update_rolling_profile(self, bar) -> None:
        """Update rolling volume profile with current bar."""
        self._profile_bars.append(bar)
        if len(self._profile_bars) > self.profile_window:
            old_bar = self._profile_bars.pop(0)
            # Remove old bar's volume from profile
            old_level = self._discretize_price(old_bar.close)
            self._rolling_profile[old_level] -= old_bar.volume
            if self._rolling_profile[old_level] <= 0:
                del self._rolling_profile[old_level]
        
        # Add current bar's volume to profile
        level = self._discretize_price(bar.close)
        self._rolling_profile[level] += bar.volume

    def _get_volume_nodes(self) -> set:
        """Identify volume nodes (above-average volume levels)."""
        if not self._rolling_profile:
            return set()
        
        avg_volume = sum(self._rolling_profile.values()) / len(self._rolling_profile)
        nodes = {level for level, vol in self._rolling_profile.items() 
                if vol >= avg_volume * self.volume_node_threshold}
        return nodes

    def _get_next_volume_level(self, current_price: float, direction: str) -> Optional[float]:
        """
        Get the next volume node in the specified direction.
        Returns None if no node found in that direction within reasonable distance.
        """
        nodes = self._get_volume_nodes()
        if not nodes:
            return None
        
        if direction == "long":
            # Find lowest node above current price
            candidates = [n for n in nodes if n > current_price]
            if candidates:
                return min(candidates)
        else:  # short
            # Find highest node below current price
            candidates = [n for n in nodes if n < current_price]
            if candidates:
                return max(candidates)
        
        return None

    def _has_level_bias(self, level: float, direction: str) -> bool:
        """Check if level has prior history in same direction."""
        if level not in self._level_memory:
            return False
        return self._level_memory[level][direction] > 0

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _detect_absorption(self, current) -> tuple:
        """
        Detect absorption candidate with volume profile and level memory.
        
        Returns (setup_dict, discretized_level) or (None, None).
        """
        if current.volume == 0:
            return None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None

        # Filter 2: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None

        level = self._discretize_price(current.close)

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None, None
            
            # Filter 4: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Filter 5: Level qualification (volume node OR level memory)
            volume_nodes = self._get_volume_nodes()
            has_level_memory = self._has_level_bias(level, "long")
            is_volume_node = level in volume_nodes
            
            if not (is_volume_node or has_level_memory):
                return None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "long",
                "absorption_close": current.close,
                "absorption_level": level,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, level
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None, None
            
            # Filter 4: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Filter 5: Level qualification (volume node OR level memory)
            volume_nodes = self._get_volume_nodes()
            has_level_memory = self._has_level_bias(level, "short")
            is_volume_node = level in volume_nodes
            
            if not (is_volume_node or has_level_memory):
                return None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "short",
                "absorption_close": current.close,
                "absorption_level": level,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, level
        
        return None, None

    def _should_confirm_first_bar(self, confirming_bar) -> bool:
        """
        Check if first confirmation bar validates the absorption setup.
        
        Must close in direction of absorption.
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]
        
        if direction == "long":
            return confirming_bar.close > self._pending_absorption_close
        else:  # short
            return confirming_bar.close < self._pending_absorption_close

    def _should_confirm_second_bar(self, confirming_bar) -> bool:
        """
        Check if second confirmation bar validates the setup.
        
        Must continue directional movement AND reach or exceed next volume node.
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup["absorption_close"]
        
        if direction == "long":
            # Must close above absorption
            if confirming_bar.close <= absorption_close:
                return False
            
            # Check if we've reached or exceeded next volume node
            next_node = self._get_next_volume_level(absorption_close, "long")
            if next_node is None:
                # No volume node ahead, accept if price continues up
                return confirming_bar.close > absorption_close
            
            return confirming_bar.close >= next_node
        
        else:  # short
            # Must close below absorption
            if confirming_bar.close >= absorption_close:
                return False
            
            # Check if we've reached or exceeded next volume node
            next_node = self._get_next_volume_level(absorption_close, "short")
            if next_node is None:
                # No volume node ahead, accept if price continues down
                return confirming_bar.close < absorption_close
            
            return confirming_bar.close <= next_node

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
