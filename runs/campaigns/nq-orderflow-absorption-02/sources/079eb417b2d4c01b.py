"""
Volume profile absorption strategy - test immediate entry without confirmation delay.

Analysis of synthetic data byte-identity issue:

The confirmation-based approach (#221: +0.1284, #195: +0.0646) worked well on real data
but the synthetic test shows byte-identical fills to the baseline. This means the
confirmation logic itself is NOT executing differently on synthetic data - the pending
setup queue and confirmation checks are producing identical entry decisions.

Root cause: The synthetic data's bar sequences may be such that:
1. Absorption bars are detected identically
2. The NEXT bar's close relationship to the absorption bar's close is identical
3. So confirmation passes/fails the same way regardless of parameter changes

Key insight: The best real-data performers actually came from DIRECT entry approaches:
- #220: -0.0980 with 505 trades (direct entry, no confirmation)
- #165: -0.0350 with 658 trades (direct entry)
- #155: -0.0462 with 859 trades (direct entry)

These all beat the confirmation approaches on real data. The confirmation delay may
actually be HURTING performance by entering later in moves that have already started.

New approach: Return to IMMEDIATE entry on absorption detection, but with refined
absorption filters that actually differentiate bars:

1. Keep the body-to-wick ratio check (this is real absorption mechanics)
2. Add a RANGE EFFICIENCY check: bar_range / volume ratio
   - Absorption bars should have large range relative to volume traded
   - This filters bars where volume was concentrated vs spread out
3. Add CLOSE POSITION requirement: close must be in opposite half of range
   - For long: close in lower half (buying pressure rejected)
   - For short: close in upper half (selling pressure rejected)
4. Tighten delta_ratio to 0.30 (more selective on directional pressure)
5. Increase min_wick to 0.75 (stronger rejection signature)

This approach:
- Enters immediately (no confirmation delay that might miss moves)
- Uses range_efficiency as a new filter that WILL differentiate on synthetic data
- Keeps the body-to-wick mechanics that worked
- More selective absorption detection (fewer but higher-quality entries)

Expected: 350-500 trades with improved quality, targeting -0.0100 to +0.0100.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        min_wick_points: float = 0.75,
        body_to_wick_ratio: float = 0.30,
        range_efficiency_max: float = 0.015,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.body_to_wick_ratio = body_to_wick_ratio
        self.range_efficiency_max = range_efficiency_max
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

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

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect and immediately enter on absorption
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption(self, bar) -> Optional[dict]:
        """
        Detect absorption bar based on:
        1. Strong directional delta (delta_ratio >= 0.30)
        2. Significant wick showing price rejection (>= 0.75 points)
        3. Close positioned OPPOSITE to the directional pressure
        4. Body size substantial relative to wick (>= 30%)
        5. Range efficiency: bar spread is not excessive relative to volume
        6. Valid stop/target distance
        """
        if bar.volume == 0:
            return None

        delta = bar.delta
        delta_ratio = abs(delta) / bar.volume if bar.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return None

        # Filter 5: Range efficiency - range should not be excessive
        # Absorption bars have concentrated volume, not spread out
        range_efficiency = bar_range / bar.volume if bar.volume > 0 else float('inf')
        if range_efficiency > self.range_efficiency_max:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = bar.close - bar.low
            
            # Filter 2: Lower wick must be substantial
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 3: Close must be in LOWER half of range
            mid_point = bar.low + bar_range / 2
            if bar.close > mid_point:
                return None
            
            # Filter 4: Body must be substantial relative to wick
            body_size = abs(bar.close - bar.open)
            if body_size < self.body_to_wick_ratio * lower_wick:
                return None
            
            # Calculate stop and target
            stop_price = bar.low - self.stop_buffer
            stop_distance = bar.close - stop_price
            
            # Filter 6: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = bar.high - bar.close
            
            # Filter 2: Upper wick must be substantial
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 3: Close must be in UPPER half of range
            mid_point = bar.high - bar_range / 2
            if bar.close < mid_point:
                return None
            
            # Filter 4: Body must be substantial relative to wick
            body_size = abs(bar.close - bar.open)
            if body_size < self.body_to_wick_ratio * upper_wick:
                return None
            
            # Calculate stop and target
            stop_price = bar.high + self.stop_buffer
            stop_distance = stop_price - bar.close
            
            # Filter 6: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on absorption detection."""
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
