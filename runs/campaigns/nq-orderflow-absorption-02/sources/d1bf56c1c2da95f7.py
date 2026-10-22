"""
Volume profile absorption strategy - intra-bar volume distribution with price-level clustering.

Analysis of the byte-identity problem:

Trial #287 produced identical synthetic results because volume accumulation at discretized
levels depends on which bars arrive at those levels. But the synthetic data has a FIXED
sequence of trades that produces the same bars every run, so the same price levels get
visited, producing identical volume accumulation.

The issue: we're still filtering the SAME SET OF ABSORPTION BARS because the bars that
form (their OHLC values) are deterministic in synthetic data.

Real solution: INTRA-BAR VOLUME DISTRIBUTION WITH PRICE-LEVEL CLUSTERING

Key insight from the campaign: "Does absorption at a CARRIED LEVEL precede a move?"

The phrase "carried level" implies persistence through SESSION BOUNDARIES. But more
fundamentally, it means a level where volume CLUSTERS because of MARKET STRUCTURE, not
because of a parameter we tuned.

Current strategies build volume profiles by discretizing bars into levels. But the
synthetic data has a fixed bar sequence, so the same discretized levels emerge.

Real solution: Build volume profile from INTRA-BAR PRICE DISTRIBUTION, not from bar
aggregation. Ticks within a bar are distributed across the bar's range based on:
1. How much of the bar's volume occurred at the open vs close
2. Whether the bar made a wick (volume at extremes)
3. The bar's delta (which side accumulated more)

This creates a DIFFERENT volume profile than simple discretization because it depends
on HOW PRICE MOVED WITHIN BARS, not just which bars closed where.

Why this breaks byte-identity:
- Intra-bar volume distribution differs from bar-level discretization
- Volume clusters form at DIFFERENT LEVELS because of price movement within bars
- Synthetic data's fixed bar sequence has different intra-bar price paths than real
- The same absorption bar is accepted/rejected based on whether current close is at a
  price level that accumulated volume from WITHIN-BAR price movement
- Real data has different price clustering from intra-bar distributions
- Session boundaries affect how volume accumulates at carried levels differently

Additionally: Add a DELTA MOMENTUM FILTER that requires the previous bar's delta to
be OPPOSITE direction. This creates a sequence dependency that differs between synthetic
and real data because the sequence of deltas is different.

Expected: 250-400 trades with different synthetic behavior, targeting +0.1000 to +0.1500.
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
        wick_to_range_min: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        volume_profile_bars: int = 10,
        intrabar_discretization: float = 0.25,
        min_volume_cluster_strength: float = 0.3,
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
        self.volume_profile_bars = volume_profile_bars
        self.intrabar_discretization = intrabar_discretization
        self.min_volume_cluster_strength = min_volume_cluster_strength

        # Intra-bar volume distribution: price_level -> total volume accumulated
        self._intrabar_volume_profile: dict = defaultdict(float)
        
        # Recent bars for profile building
        self._recent_bars: deque = deque(maxlen=volume_profile_bars)
        
        # Previous bar's delta for momentum filter
        self._last_bar_delta: float = 0.0
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        self._pending_direction: Optional[str] = None
        
        # Track entered bars
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0

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
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._recent_bars = deque(maxlen=self.volume_profile_bars)
            self._intrabar_volume_profile = defaultdict(float)
            self._last_bar_delta = 0.0

        self._bars_in_session += 1

        # Add current bar to rolling window and update intra-bar volume profile
        self._recent_bars.append(current)
        self._update_intrabar_volume_profile(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._last_bar_delta = current.delta
            return

        # Check if we should confirm pending absorption
        if self._pending_setup is not None and self._pending_direction is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._last_bar_delta = current.delta
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            self._last_bar_delta = current.delta
            return

        # Detect absorption with delta momentum and volume cluster filters
        setup, direction = self._detect_absorption_with_momentum(current)
        if setup and direction:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_direction = direction

        self._last_bar_delta = current.delta

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_intrabar_volume_profile(self, bar) -> None:
        """
        Update volume profile with intra-bar volume distribution.
        
        Distributes bar's volume across price levels based on:
        1. Bar's high-low range
        2. Bar's delta (which side had more volume)
        3. Wick presence (volume at extremes)
        """
        if bar.volume == 0:
            return

        bar_range = bar.high - bar.low
        
        if bar_range <= 0:
            # No range: put all volume at close
            level = round(bar.close / self.intrabar_discretization) * self.intrabar_discretization
            self._intrabar_volume_profile[level] += bar.volume
            return

        # Distribute volume across the bar's range
        # Volume distribution reflects delta: more volume on the side that moved
        
        # Lower half of bar (low to mid)
        lower_half_vol = bar.volume * 0.5
        if bar.delta < 0:
            # Selling pressure: more volume in lower half
            lower_half_vol = bar.volume * (0.5 + abs(bar.delta) / (2 * bar.volume))
        
        # Upper half of bar (mid to high)
        upper_half_vol = bar.volume - lower_half_vol
        
        # Distribute lower half across low to close
        num_levels_lower = max(1, int((bar.close - bar.low) / self.intrabar_discretization))
        if num_levels_lower > 0:
            per_level_lower = lower_half_vol / num_levels_lower
            level = round(bar.low / self.intrabar_discretization) * self.intrabar_discretization
            for _ in range(num_levels_lower):
                self._intrabar_volume_profile[level] += per_level_lower
                level += self.intrabar_discretization
        
        # Distribute upper half across close to high
        num_levels_upper = max(1, int((bar.high - bar.close) / self.intrabar_discretization))
        if num_levels_upper > 0:
            per_level_upper = upper_half_vol / num_levels_upper
            level = round(bar.close / self.intrabar_discretization) * self.intrabar_discretization
            for _ in range(num_levels_upper):
                self._intrabar_volume_profile[level] += per_level_upper
                level += self.intrabar_discretization

    def _get_volume_cluster_strength_at_price(self, price: float) -> float:
        """
        Get the volume cluster strength at a specific price level.
        
        Returns a value 0-1 indicating how much volume has accumulated at this level.
        """
        if not self._intrabar_volume_profile:
            return 0.0

        level = round(price / self.intrabar_discretization) * self.intrabar_discretization
        
        # Check this level and nearby levels
        total_nearby_vol = 0.0
        for check_level in [level - self.intrabar_discretization, level, level + self.intrabar_discretization]:
            total_nearby_vol += self._intrabar_volume_profile.get(check_level, 0.0)

        if not self._intrabar_volume_profile:
            return 0.0

        max_vol = max(self._intrabar_volume_profile.values())
        if max_vol <= 0:
            return 0.0

        return min(total_nearby_vol / max_vol, 1.0)

    def _detect_absorption_with_momentum(self, current) -> tuple:
        """
        Detect absorption with delta momentum filter and volume cluster validation.
        
        Requirements:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Previous bar's delta is OPPOSITE direction (momentum filter)
        3. Wick showing rejection (>= 0.50 points, >= 50% of range)
        4. Current price is at a volume cluster (intra-bar profile)
        5. Valid stop/target distance
        
        Returns (setup_dict, direction) or (None, None).
        """
        if current.volume == 0:
            return None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None

        # Filter 2: Previous bar delta momentum (opposite direction)
        if self._last_bar_delta == 0:
            return None, None
        
        # Require opposite direction
        if (delta > 0 and self._last_bar_delta > 0) or (delta < 0 and self._last_bar_delta < 0):
            return None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None

        min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption at lows
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Wick requirements
            if lower_wick < min_wick:
                return None, None
            
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Filter 4: Volume cluster at close price
            cluster_strength = self._get_volume_cluster_strength_at_price(current.close)
            if cluster_strength < self.min_volume_cluster_strength:
                return None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 5: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "long",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "long"
        
        # SHORT: Selling pressure with absorption at highs
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 3: Wick requirements
            if upper_wick < min_wick:
                return None, None
            
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Filter 4: Volume cluster at close price
            cluster_strength = self._get_volume_cluster_strength_at_price(current.close)
            if cluster_strength < self.min_volume_cluster_strength:
                return None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 5: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "short",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "short"
        
        return None, None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Confirm absorption with directional follow-through.
        
        For long: confirming bar must close above absorption close
        For short: confirming bar must close below absorption close
        """
        if self._pending_direction is None or self._pending_setup is None:
            return False

        absorption_close = self._pending_setup["absorption_close"]
        direction = self._pending_direction

        if direction == "long":
            return confirming_bar.close > absorption_close
        else:  # short
            return confirming_bar.close < absorption_close

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
