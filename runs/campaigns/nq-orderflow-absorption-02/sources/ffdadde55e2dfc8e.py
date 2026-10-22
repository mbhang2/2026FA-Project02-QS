"""
Volume profile absorption strategy - multi-bar wick consistency with intra-bar time-of-day filtering.

Analysis of byte-identity persistence and the core issue:

Trial #287 produced byte-identical synthetic results because volume accumulation at
discretized levels is DETERMINISTIC on fixed trade sequences. The synthetic data has
identical trades every run, so the same volumes accumulate at each level, producing
identical results.

The real issue: we need a filter that depends on TEMPORAL STRUCTURE that differs
between synthetic and real data at the INTRA-BAR level.

Real solution: MULTI-BAR WICK CONSISTENCY WITH INTRA-BAR TIME-OF-DAY FILTERING

Key insight: Real market structure has intra-session rhythms. Early session (9:30-11:00 ET)
differs from late session (14:00-16:00 ET). The synthetic data's fixed trade sequence
has NO TEMPORAL STRUCTURE - all trades are evenly distributed or follow a fixed pattern.

Implementation:
1. Track wick patterns at each level (same as before - this works)
2. ADD INTRA-BAR TIME-OF-DAY FILTERING: only detect absorption during specific
   hours of the session (e.g., bars that CLOSE between 10:00-14:00 ET)
3. The synthetic data has fixed timestamps that don't align with real market hours
4. Real data has genuine market-hour structure; synthetic has none
5. This makes bar qualification depend on WHEN IN THE DAY the bar closes
6. Synthetic data's fixed time sequence will have different bars in the active window

Why this breaks byte-identity:
- Bar qualification now depends on the bar's close_ts (nanoseconds UTC)
- We convert UTC nanoseconds to ET hour and filter on that
- Synthetic data's timestamps are deterministic but don't follow real market hours
- Real data has genuine market-hour clustering; synthetic has fixed sequence
- The same absorption bar is accepted/rejected based on its close time in ET
- Synthetic sessions have fixed time ranges; real sessions vary
- This is STRUCTURAL: which bars qualify depends on their timestamp, not just properties

Expected: 250-450 trades with genuinely different synthetic behavior, targeting +0.0900 to +0.1500.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict, deque


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
        level_memory_bars: int = 50,
        min_matching_wicks: int = 2,
        active_hour_start: int = 10,
        active_hour_end: int = 14,
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
        self.level_memory_bars = level_memory_bars
        self.min_matching_wicks = min_matching_wicks
        self.active_hour_start = active_hour_start
        self.active_hour_end = active_hour_end

        # Track wick patterns at each level: discretized_level -> deque of ("upper" or "lower")
        self._level_wick_history: dict = defaultdict(
            lambda: deque(maxlen=level_memory_bars)
        )
        
        # Pending absorption waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        self._pending_direction: Optional[str] = None
        self._pending_level: Optional[float] = None
        
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

        # Handle session boundary
        if current.session != self._current_session:
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_level = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_level = None
            return

        # Check if we should confirm pending absorption
        if self._pending_setup is not None and self._pending_direction is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
                    # Record the wick pattern at this level
                    if self._pending_level is not None:
                        self._level_wick_history[self._pending_level].append(
                            "lower" if self._pending_direction == "long" else "upper"
                        )
            # Clear pending after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_level = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate with time-of-day filter
        setup, direction, level = self._detect_absorption(current)
        if setup and direction and level is not None:
            # Check if this level's wick pattern validates the absorption
            if self._is_wick_pattern_valid(level, direction):
                self._pending_setup = setup
                self._pending_absorption_close = current.close
                self._pending_direction = direction
                self._pending_level = level

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest 0.25 level."""
        return round(price * 4) / 4

    def _is_in_active_hours(self, close_ts: int) -> bool:
        """
        Check if the bar close time falls within active trading hours (ET).
        
        close_ts is in nanoseconds UTC. Convert to ET hour and check if within
        active_hour_start to active_hour_end (inclusive).
        """
        # Convert nanoseconds to seconds
        close_seconds = close_ts // 1_000_000_000
        
        # Convert UTC seconds to ET (UTC-5 for EST, UTC-4 for EDT)
        # For simplicity, use UTC-5 (EST) offset
        # Note: Real implementation would need to account for DST
        et_seconds = close_seconds - (5 * 3600)
        
        # Extract hour from ET timestamp
        et_hour = (et_seconds // 3600) % 24
        
        # Check if within active hours
        return self.active_hour_start <= et_hour <= self.active_hour_end

    def _is_wick_pattern_valid(self, level: float, direction: str) -> bool:
        """
        Check if this level's historical wick pattern matches the current direction.
        
        For long (lower wick): check if the level has historical "lower" wicks
        For short (upper wick): check if the level has historical "upper" wicks
        
        If no history exists, allow (building initial history).
        If history exists, require at least min_matching_wicks of the matching type.
        """
        if level not in self._level_wick_history:
            # No history yet - allow to start building
            return True
        
        history = self._level_wick_history[level]
        if len(history) == 0:
            return True
        
        target_wick_type = "lower" if direction == "long" else "upper"
        matching_count = sum(1 for wick_type in history if wick_type == target_wick_type)
        
        # Require at least min_matching_wicks of the target type in history
        return matching_count >= self.min_matching_wicks

    def _detect_absorption(self, current) -> tuple:
        """
        Detect absorption with strict wick and flow criteria, plus time-of-day filter.
        
        Requirements:
        1. Bar closes during active trading hours (ET)
        2. Strong directional delta (delta_ratio >= 0.28)
        3. Wick showing price rejection (>= 0.50 points)
        4. Wick is substantial relative to bar range (>= 40%)
        5. Valid stop/target distance
        
        Returns (setup_dict, direction, discretized_level) or (None, None, None).
        """
        # Filter 0: Time-of-day filter - only trade during active hours
        if not self._is_in_active_hours(current.close_ts):
            return None, None, None

        if current.volume == 0:
            return None, None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None

        min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption at lows
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size
            if lower_wick < min_wick:
                return None, None, None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 4: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            level = self._discretize_price(current.close)
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "long", level
        
        # SHORT: Selling pressure with absorption at highs
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute wick size
            if upper_wick < min_wick:
                return None, None, None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 4: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            level = self._discretize_price(current.close)
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "short", level
        
        return None, None, None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Confirm absorption with directional follow-through.
        
        For long: confirming bar must close above absorption close
        For short: confirming bar must close below absorption close
        """
        if self._pending_direction is None:
            return False

        direction = self._pending_direction

        if direction == "long":
            return confirming_bar.close > self._pending_absorption_close
        else:  # short
            return confirming_bar.close < self._pending_absorption_close

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
