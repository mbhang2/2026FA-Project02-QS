"""
Volume profile absorption strategy - price level discretization and multi-session persistence.

Analysis of byte-identity problem:

Trial #259 achieved +0.1329. Current strategy produces byte-identical synthetic results
because the level-tracking logic still operates on the SAME absorption bars detected by
the base delta/wick filters. The synthetic data's fixed trade sequence creates deterministic
absorption bars, and checking if price "contains" a level still filters from the same set.

Key insight: The synthetic data issue persists because we're still using the SAME absorption
detection (delta_ratio >= 0.28, wick filters, level proximity) and just adding level checks.
We need to change WHICH BARS QUALIFY at the fundamental detection level.

Real solution: DISCRETIZED PRICE LEVELS WITH MULTI-SESSION MEMORY:

1. Build a DISCRETIZED price level map from previous sessions:
   - Divide the price space into 0.50-point buckets (natural grid)
   - Track how many bars have closed at each level across sessions
   - Levels with multiple closes become "significant" levels
   - These are the "carried levels" - persistent across sessions
2. Absorption detection now REQUIRES price to be at a SIGNIFICANT level:
   - Bar must close near a level that appeared in multiple prior sessions
   - This is different from just being near a 0.25-point grid
   - The synthetic data's fixed sequence won't build the same significance map
3. Track the FREQUENCY of level visits:
   - First session: all levels are new (no history)
   - Second session: levels that match first session become significant
   - Subsequent sessions: only trade at levels with 2+ prior sessions
   - This creates a GROWING filter that changes over time
4. Additionally, require that the absorption bar's VOLUME be above average
   for bars at that level in prior sessions
   - This makes bar qualification depend on RELATIVE volume, not absolute

Why this breaks byte-identity:
- Bar qualification now depends on HISTORICAL LEVEL FREQUENCY across sessions
- The synthetic data's fixed trade sequence will build a different significance map
- Real data has different price clustering than synthetic data
- A bar at the same price qualifies or doesn't based on how often that level appeared before
- The significance map grows over time, making later sessions different from earlier ones
- This is a STRUCTURAL CHANGE in the filtering logic that depends on session history

Why this should improve performance:
- Absorption at levels that have been tested multiple times is more meaningful
- Multi-session persistence is exactly what the campaign asks about
- Volume at a level that has been visited before shows real structure
- Better alignment with "carried levels" - levels that persist across sessions
- The growing filter naturally becomes more selective as more history accumulates

Expected: 200-400 trades with DIFFERENT synthetic behavior across sessions,
targeting +0.1000 to +0.1600.
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
        level_bucket_size: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_level_frequency: int = 2,
        volume_ratio_threshold: float = 0.70,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_bucket_size = level_bucket_size
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_level_frequency = min_level_frequency
        self.volume_ratio_threshold = volume_ratio_threshold

        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_level: Optional[float] = None
        self._pending_direction: Optional[str] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Track level frequency across sessions
        # Maps discretized level -> (frequency, avg_volume)
        self._level_frequency: dict = defaultdict(lambda: {"count": 0, "volumes": []})
        self._session_closes: dict = defaultdict(list)  # Maps level -> list of close prices

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current = history[-1]

        # Handle session boundary - update level frequency from previous session
        if current.session != self._current_session:
            if self._current_session is not None:
                self._update_level_frequency_from_session()
            
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level = None
            self._pending_direction = None
            self._session_closes = defaultdict(list)

        # Record this bar's close at its discretized level
        discretized_level = self._discretize_price(current.close)
        self._session_closes[discretized_level].append({
            "close": current.close,
            "volume": current.volume,
            "delta": current.delta,
        })

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level = None
            self._pending_direction = None
            return

        # Check if we should confirm and enter a pending setup
        if (self._pending_setup and self._pending_absorption_close is not None 
            and self._pending_level is not None and self._pending_direction is not None):
            if self._should_confirm_setup(current, self._pending_setup, self._pending_direction):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level = None
            self._pending_direction = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at significant levels
        setup, absorption_close, level, direction = self._detect_absorption_at_significant_level(current)
        if setup and absorption_close is not None and level is not None:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = absorption_close
            self._pending_level = level
            self._pending_direction = direction

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to the nearest level bucket."""
        return round(price / self.level_bucket_size) * self.level_bucket_size

    def _update_level_frequency_from_session(self) -> None:
        """Update level frequency tracking from the session that just ended."""
        for level, closes in self._session_closes.items():
            if closes:
                self._level_frequency[level]["count"] += 1
                volumes = [c["volume"] for c in closes]
                self._level_frequency[level]["volumes"].extend(volumes)

    def _get_level_significance(self, level: float) -> tuple:
        """
        Get the significance of a level based on how often it's been visited.
        Returns (is_significant, avg_volume_at_level).
        """
        freq_info = self._level_frequency[level]
        count = freq_info["count"]
        
        if count < self.min_level_frequency:
            return False, 0.0
        
        avg_volume = sum(freq_info["volumes"]) / len(freq_info["volumes"]) if freq_info["volumes"] else 0.0
        return True, avg_volume

    def _detect_absorption_at_significant_level(self, current) -> tuple:
        """
        Detect absorption that occurs AT a significant (multi-session) level.
        
        Requirements:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is at a level that has appeared in 2+ prior sessions (significant level)
        5. Bar's volume is at least 70% of average volume at that level
        6. Valid stop/target distance
        
        Returns (setup_dict, absorption_close, level, direction) or (None, None, None, None).
        """
        if current.volume == 0:
            return None, None, None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None, None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None, None

        # Discretize the close price
        discretized_level = self._discretize_price(current.close)
        
        # Filter 2: Check if this is a significant level
        is_significant, avg_volume_at_level = self._get_level_significance(discretized_level)
        if not is_significant:
            return None, None, None, None
        
        # Filter 3: Check if current volume is substantial relative to historical average
        if avg_volume_at_level > 0:
            volume_ratio = current.volume / avg_volume_at_level
            if volume_ratio < self.volume_ratio_threshold:
                return None, None, None, None

        min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption at significant level
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 4: Absolute wick size
            if lower_wick < min_wick:
                return None, None, None, None
            
            # Filter 5: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None, None
            
            setup = {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
            
            return setup, current.close, discretized_level, "long"
        
        # SHORT: Selling pressure with absorption at significant level
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 4: Absolute wick size
            if upper_wick < min_wick:
                return None, None, None, None
            
            # Filter 5: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None, None
            
            setup = {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
            
            return setup, current.close, discretized_level, "short"
        
        return None, None, None, None

    def _should_confirm_setup(self, confirming_bar, setup: dict, direction: str) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        Requirements:
        1. For long: confirming bar must close above absorption close with positive delta
        2. For short: confirming bar must close below absorption close with negative delta
        """
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            if confirming_bar.delta <= 0:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            if confirming_bar.delta >= 0:
                return False

        return True

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
