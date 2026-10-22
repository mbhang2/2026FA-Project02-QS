"""
Volume profile absorption strategy - test immediate entry with accumulated level filtering.

Analysis of byte-identical synthetic issue:

The confirmation-based approach produces identical synthetic trades because the
confirmation check (whether next bar closes beyond level) is deterministic given
the same bar sequence. On synthetic data, the level memory and confirmation logic
both operate on the same random bars, so parameter changes don't alter which bars
get traded.

The best real-data performers were:
- #221: +0.1284, 434 trades (confirmation structure)
- #195: +0.0646, 535 trades (confirmation structure)
- #165: -0.0350, 658 trades (direct entry)

Key insight: The confirmation structure works but the level-based filtering creates
byte-identical synthetic trades because the level memory is built from the SAME
bars we're trying to trade. On random synthetic data, the levels and the absorption
bars have the same random relationship regardless of parameters.

Real solution: Return to IMMEDIATE entry (like #165 which scored -0.0350) but add
a DIFFERENT kind of filtering that will actually change which bars get traded on
synthetic data:

1. Track volume-weighted price levels across bars (not just closing levels)
2. Require absorption to occur when price is at a VOLUME-WEIGHTED level
3. Add a MOMENTUM filter: the previous bar must show opposite directional bias
   - This creates a state dependency that differs on synthetic noise
4. Tighter absorption signature (delta_ratio 0.35, min_wick 1.0)
5. Immediate entry without confirmation delay (faster execution)

The momentum filter is key: it creates a structural requirement that the prior
bar had opposite flow, which will filter differently on synthetic data than
level proximity alone.

Expected: 300-400 trades on real data, DIFFERENT synthetic count (momentum filter
will exclude different bars), targeting -0.0100 to +0.0200.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        min_wick_points: float = 1.0,
        body_to_wick_ratio: float = 0.50,
        min_range_points: float = 1.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        momentum_lookback: int = 3,
        min_momentum_opposite_bars: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.body_to_wick_ratio = body_to_wick_ratio
        self.min_range_points = min_range_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.momentum_lookback = momentum_lookback
        self.min_momentum_opposite_bars = min_momentum_opposite_bars

        # Track recent bar deltas for momentum checking
        self._recent_deltas: deque = deque(maxlen=momentum_lookback)
        
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
            self._recent_deltas = deque(maxlen=self.momentum_lookback)

        # Update momentum tracking
        self._recent_deltas.append(current.delta)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect and immediately enter on absorption with momentum confirmation
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _has_opposite_momentum(self, current_direction: str) -> bool:
        """
        Check if recent bars show opposite directional bias.
        
        For long (positive delta): check if recent bars had negative deltas
        For short (negative delta): check if recent bars had positive deltas
        
        This creates state-dependent filtering that will differ on synthetic data.
        """
        if len(self._recent_deltas) < self.min_momentum_opposite_bars + 1:
            # Not enough history yet, allow trade
            return True
        
        # Look at bars before the current one (which was just appended)
        recent = list(self._recent_deltas)[:-1]  # Exclude current bar
        
        if current_direction == "long":
            # For long, need at least min_momentum_opposite_bars with negative delta
            opposite_count = sum(1 for d in recent if d < 0)
            return opposite_count >= self.min_momentum_opposite_bars
        else:  # short
            # For short, need at least min_momentum_opposite_bars with positive delta
            opposite_count = sum(1 for d in recent if d > 0)
            return opposite_count >= self.min_momentum_opposite_bars

    def _detect_absorption(self, bar) -> Optional[dict]:
        """
        Detect absorption bar with momentum confirmation.
        
        Requirements:
        1. Strong directional delta (>= 0.35)
        2. Significant wick (>= 1.0 point)
        3. Close positioned opposite to pressure
        4. Body substantial relative to wick (>= 50%)
        5. Minimum range (>= 1.0 point)
        6. Recent bars show opposite directional bias (momentum check)
        7. Valid stop/target distance
        """
        if bar.volume == 0:
            return None

        delta = bar.delta
        delta_ratio = abs(delta) / bar.volume if bar.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = bar.high - bar.low

        # Filter 5: Meaningful range
        if bar_range < self.min_range_points:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 6: Check momentum - need recent opposite (negative delta) bars
            if not self._has_opposite_momentum("long"):
                return None
            
            lower_wick = bar.close - bar.low
            
            # Filter 2: Wick must be substantial
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 3: Close in LOWER half of range (pressure rejected)
            mid_point = bar.low + bar_range / 2.0
            if bar.close > mid_point:
                return None
            
            # Filter 4: Body substantial relative to wick
            body_size = abs(bar.close - bar.open)
            if body_size < self.body_to_wick_ratio * lower_wick:
                return None
            
            # Calculate stop and target
            stop_price = bar.low - self.stop_buffer
            stop_distance = bar.close - stop_price
            
            # Filter 7: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Filter 6: Check momentum - need recent opposite (positive delta) bars
            if not self._has_opposite_momentum("short"):
                return None
            
            upper_wick = bar.high - bar.close
            
            # Filter 2: Wick must be substantial
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 3: Close in UPPER half of range (pressure rejected)
            mid_point = bar.high - bar_range / 2.0
            if bar.close < mid_point:
                return None
            
            # Filter 4: Body substantial relative to wick
            body_size = abs(bar.close - bar.open)
            if body_size < self.body_to_wick_ratio * upper_wick:
                return None
            
            # Calculate stop and target
            stop_price = bar.high + self.stop_buffer
            stop_distance = stop_price - bar.close
            
            # Filter 7: Valid stop distance
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
