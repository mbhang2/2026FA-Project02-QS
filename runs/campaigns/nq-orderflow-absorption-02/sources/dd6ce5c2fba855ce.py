"""
Volume profile absorption strategy - test bar body efficiency and flow quality.

Analysis of persistent byte-identical issue:
The efficiency_multiplier approach in the previous trial still produced identical trades,
which suggests the efficiency calculation itself may not be differentiating bars
effectively on synthetic data, or the rolling averages are too similar.

Key insight: The best performers (#81: -0.0096, #165: -0.0350, #155: -0.0462) all
trade absorption bars directly without complex confirmation or efficiency filters.
The byte-identical results across many parameter changes suggest the real issue is
that we're not actually FILTERING differently - we're just computing different
numbers that don't change the entry decisions.

New approach: Instead of efficiency ratios, use bar BODY SIZE relative to RANGE.
This is simpler, more direct, and will actually filter different bars:

1. Absorption requires not just a wick, but a STRONG BODY in the opposite direction
   of the wick (this is what makes it "absorption" - flow that doesn't move price)
2. For long (buying pressure): require close to be in LOWER half of range, not upper
   This means buying volume pushed price up but closed low = absorption
3. For short (selling pressure): require close to be in UPPER half of range, not lower
   This means selling volume pushed price down but closed high = absorption

4. Add a body quality check: the bar's body (|close - open|) must be substantial
   relative to the wick, showing real absorption mechanics

This directly tests the campaign hypothesis: "Does absorption at a level precede
a move away?" By requiring the close to be OPPOSITE to the directional pressure,
we're identifying bars where flow failed to move price - the core absorption signal.

For a long setup (buy pressure):
- delta > 0 (net buy flow)
- lower wick >= 0.50 points (price rejection downward)
- close must be in LOWER HALF of range (close < (open + close)/2 relative to high)
- body size (|close - open|) must be >= 30% of wick size (real absorption, not just wick)

For a short setup (sell pressure):
- delta < 0 (net sell flow)
- upper wick >= 0.50 points (price rejection upward)  
- close must be in UPPER HALF of range (close > (open + close)/2 relative to low)
- body size (|close - open|) must be >= 30% of wick size (real absorption, not just wick)

Expected: 400-600 trades with much more selective absorption detection, targeting -0.0150 to -0.0250.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        body_to_wick_ratio: float = 0.30,
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
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

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

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption on current bar
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
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Significant wick showing price rejection (>= 0.50 points)
        3. Close positioned OPPOSITE to the directional pressure
        4. Body size substantial relative to wick (>= 30%)
        5. Valid stop/target distance
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

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = bar.close - bar.low
            
            # Filter 2: Lower wick must be substantial
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 3: Close must be in LOWER half of range
            # This means buying pressure didn't sustain the move
            mid_point = bar.low + bar_range / 2
            if bar.close > mid_point:
                # Close is in upper half - not absorption
                return None
            
            # Filter 4: Body must be substantial relative to wick
            body_size = abs(bar.close - bar.open)
            if body_size < self.body_to_wick_ratio * lower_wick:
                return None
            
            # Calculate stop and target
            stop_price = bar.low - self.stop_buffer
            stop_distance = bar.close - stop_price
            
            # Filter 5: Valid stop distance
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
            # This means selling pressure didn't sustain the move
            mid_point = bar.high - bar_range / 2
            if bar.close < mid_point:
                # Close is in lower half - not absorption
                return None
            
            # Filter 4: Body must be substantial relative to wick
            body_size = abs(bar.close - bar.open)
            if body_size < self.body_to_wick_ratio * upper_wick:
                return None
            
            # Calculate stop and target
            stop_price = bar.high + self.stop_buffer
            stop_distance = stop_price - bar.close
            
            # Filter 5: Valid stop distance
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
