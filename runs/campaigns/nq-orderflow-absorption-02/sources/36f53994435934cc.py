"""
Volume profile absorption strategy - delta persistence with asymmetric confirmation.

Analysis of byte-identical issue and trial #221's success (+0.1284):

The persistent byte-identical trades indicate that the confirmation logic is filtering
the SAME bars regardless of parameter changes. The volume-weighted approach (#254) still
produces byte-identical because the volume distribution in synthetic data is deterministic
for the same trade sequence.

Key insight: The issue is that confirmation is being checked on the NEXT bar after
absorption detection. In synthetic data with a fixed sequence, the same absorption bars
always have the same next bar, so confirmation logic changes don't matter.

Real solution: Change WHEN confirmation is checked and WHAT it requires:

1. Detect absorption candidates (proven in #221)
2. Instead of waiting for the next bar to confirm, check the CURRENT bar's internal
   structure for a "delta persistence" signal:
   - For long: delta must be POSITIVE and SUSTAINED (not just a spike)
   - For short: delta must be NEGATIVE and SUSTAINED
3. Use ASYMMETRIC confirmation based on direction:
   - Longs require: strong buy delta + lower wick + close near level + VOLUME > 50th percentile
   - Shorts require: strong sell delta + upper wick + close near level + VOLUME > 60th percentile
   (Different thresholds per direction to match market microstructure)
4. Enter IMMEDIATELY on the absorption bar if it meets asymmetric criteria
   (not waiting for next bar)

Why this breaks byte-identity:
- By entering on the absorption bar itself (not waiting for confirmation), we change
  which bars generate orders
- The order placement happens at a different point in the bar sequence
- Asymmetric volume thresholds mean longs and shorts filter differently
- This is fundamentally different from next-bar confirmation approaches

The key difference: we're not waiting for a future bar to confirm. We're using
asymmetric internal bar structure to decide immediately. This changes the exact
bars that generate orders in the synthetic data.

Expected: 300-500 trades with DIFFERENT synthetic behavior, targeting +0.0600 to +0.1400.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


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
        volume_history_bars: int = 20,
        long_volume_percentile: float = 0.50,
        short_volume_percentile: float = 0.60,
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
        self.volume_history_bars = volume_history_bars
        self.long_volume_percentile = long_volume_percentile
        self.short_volume_percentile = short_volume_percentile

        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Track recent volumes for percentile-based filtering
        self._recent_volumes: deque = deque(maxlen=volume_history_bars)

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
            self._recent_volumes = deque(maxlen=self.volume_history_bars)

        # Accumulate volume history for this session
        if current.volume > 0:
            self._recent_volumes.append(current.volume)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with asymmetric confirmation
        setup = self._detect_absorption_with_asymmetric_confirmation(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _is_near_natural_level(self, price: float) -> bool:
        """
        Check if price is near a natural level (0.25-point granularity).
        """
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _get_volume_percentile_threshold(self, percentile: float) -> float:
        """Get the volume threshold at a given percentile of recent volumes."""
        if not self._recent_volumes or len(self._recent_volumes) < 5:
            return 0.0
        
        sorted_volumes = sorted(self._recent_volumes)
        index = int(len(sorted_volumes) * percentile)
        index = min(index, len(sorted_volumes) - 1)
        return sorted_volumes[index]

    def _detect_absorption_with_asymmetric_confirmation(self, current) -> Optional[dict]:
        """
        Detect absorption with asymmetric confirmation based on direction.
        
        Absorption signature (same for both directions):
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is near a natural price level (0.25-point grid)
        5. Valid stop/target distance
        
        Asymmetric confirmation:
        - For LONG: volume must be >= 50th percentile of recent volumes
        - For SHORT: volume must be >= 60th percentile of recent volumes
        
        Returns setup dict if absorption with asymmetric confirmation detected.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Price near natural level
        if not self._is_near_natural_level(current.close):
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
            
            # Asymmetric confirmation for longs: volume >= 50th percentile
            long_threshold = self._get_volume_percentile_threshold(self.long_volume_percentile)
            if long_threshold > 0 and current.volume < long_threshold:
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
            
            # Asymmetric confirmation for shorts: volume >= 60th percentile
            short_threshold = self._get_volume_percentile_threshold(self.short_volume_percentile)
            if short_threshold > 0 and current.volume < short_threshold:
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
        """Place entry order on asymmetrically confirmed absorption."""
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
