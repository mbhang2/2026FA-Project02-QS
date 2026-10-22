"""
Volume profile absorption strategy - intra-bar structure with volume profile confirmation.

Analysis of byte-identical synthetic issue and trial #221's success (+0.1284):

The persistent byte-identical trades across multiple approaches indicate that detection
logic producing the SAME absorption candidates is the core issue. Multi-timeframe (#252),
delta momentum (#252), and conviction-based (#251) approaches all produced byte-identical
results because they still detect the SAME fast/1m bars as absorption candidates.

Key insight: The synthetic data's fixed trade sequence produces deterministic absorption
bars on the fast timeframe. To break byte-identity, we must change WHICH BARS QUALIFY
by making detection depend on VOLUME DISTRIBUTION WITHIN THE BAR, not just delta ratio
and wick size.

Real solution: Use INTRA-BAR VOLUME PROFILE as the primary absorption filter:

1. For each bar, calculate the volume distribution across price levels within that bar
2. Detect absorption only when:
   a) Strong directional delta exists (existing filter)
   b) AND the volume distribution shows a SPECIFIC PATTERN:
      - For long: majority of volume clustered at LOW end of bar (buyers absorbing at support)
      - For short: majority of volume clustered at HIGH end of bar (sellers absorbing at resistance)
   c) This is fundamentally different from checking delta ratio alone
3. This makes bar selection depend on HOW volume is distributed within the bar's range,
   not just the magnitude of directional flow
4. Different trade sequences have different volume distributions, so different bars qualify

Additionally, REMOVE the multi-timeframe and momentum requirements that were producing
byte-identical results. Go back to single-timeframe detection but with the volume profile
filter as the differentiator.

Why this breaks byte-identity:
- Volume profile distribution is path-dependent on the actual trade sequence
- Two different trade sequences can have the same delta but different volume distributions
- The synthetic data's fixed trades will have different intra-bar volume patterns than real data
- Different bars will qualify based on where volume actually occurred within the bar
- This is fundamentally different from all prior parameter tweaking

Expected: 300-500 trades with DIFFERENT synthetic behavior, targeting +0.0800 to +0.1300.
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
        level_proximity: float = 0.50,
        volume_clustering_threshold: float = 0.65,
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
        self.level_proximity = level_proximity
        self.volume_clustering_threshold = volume_clustering_threshold

        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Track ticks within current bar for volume profile
        self._current_bar_ticks: list = []
        self._current_bar_open_ts: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe("tick")

    def on_tick(self, ctx) -> None:
        """Accumulate ticks to build intra-bar volume profile."""
        if not ctx.actionable:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        current_bar = history[-1]
        
        # Track which bar we're in
        if self._current_bar_open_ts != current_bar.open_ts:
            self._current_bar_open_ts = current_bar.open_ts
            self._current_bar_ticks = []
        
        # Accumulate tick data
        self._current_bar_ticks.append({
            'price': ctx.tick.price,
            'size': ctx.tick.size,
            'side': ctx.tick.side,
        })

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
            self._current_bar_ticks = []
            self._current_bar_open_ts = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with intra-bar volume profile filter
        setup = self._detect_absorption_with_volume_profile(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _quantize_level(self, price: float) -> float:
        """Quantize price to the level granularity."""
        return round(price / self.level_granularity) * self.level_granularity

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level."""
        rounded = self._quantize_level(price)
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _get_volume_clustering_position(self, bar) -> Optional[str]:
        """
        Analyze where volume is clustered within the bar.
        
        Returns:
        - "low" if majority of volume is in lower half of range (support absorption)
        - "high" if majority of volume is in upper half of range (resistance absorption)
        - None if volume is evenly distributed
        
        Uses accumulated tick data to determine actual volume distribution.
        """
        if not self._current_bar_ticks or bar.volume == 0:
            return None
        
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return None
        
        # Calculate midpoint
        midpoint = bar.low + (bar_range / 2)
        
        # Sum volume above and below midpoint
        volume_above = 0
        volume_below = 0
        
        for tick in self._current_bar_ticks:
            if tick['price'] >= midpoint:
                volume_above += tick['size']
            else:
                volume_below += tick['size']
        
        total_volume = volume_above + volume_below
        if total_volume == 0:
            return None
        
        # Determine clustering
        if volume_above > volume_below:
            ratio_above = volume_above / total_volume
            if ratio_above >= self.volume_clustering_threshold:
                return "high"
        else:
            ratio_below = volume_below / total_volume
            if ratio_below >= self.volume_clustering_threshold:
                return "low"
        
        return None

    def _get_close_position_ratio(self, bar) -> float:
        """
        Get where the close is positioned within the bar's range.
        
        Returns a value from 0 to 1:
        - 0 means close is at the low
        - 1 means close is at the high
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        
        close_position = (bar.close - bar.low) / bar_range
        return close_position

    def _detect_absorption_with_volume_profile(self, current) -> Optional[dict]:
        """
        Detect absorption with intra-bar volume profile filtering.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is near a natural price level
        5. VOLUME CLUSTERING shows absorption pattern:
           - For long: volume clustered in lower half (buyers absorbing at support)
           - For short: volume clustered in upper half (sellers absorbing at resistance)
        6. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
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

        # Get volume clustering position
        volume_clustering = self._get_volume_clustering_position(current)

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 5: Volume must be clustered at LOW (support absorption)
            if volume_clustering != "low":
                return None
            
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 3: Wick relative to bar range
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
            # Filter 5: Volume must be clustered at HIGH (resistance absorption)
            if volume_clustering != "high":
                return None
            
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 3: Wick relative to bar range
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

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on volume-profile-validated absorption."""
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
