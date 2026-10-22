"""
Volume profile absorption strategy - test delta reversal with strict wick validation.

Analysis of persistent byte-identical synthetic issue:

The accumulated delta approach (#233) still produces byte-identical trades because
the accumulated_ratio check (comparing sum of deltas to total volume) is deterministic
on fixed synthetic data. The sequence of deltas and volumes is fixed, so the same
bars pass the accumulation filter regardless of parameter changes.

Root cause: Even accumulation-based logic becomes deterministic on synthetic data
when the underlying bar sequence is fixed. We need a filter that depends on RELATIVE
CHANGES between bars, not absolute accumulated values.

Real solution: Detect absorption by looking for DELTA REVERSALS - when the sign of
delta changes sharply between bars. This creates a truly different filtering pattern:

1. Track the SIGN and MAGNITUDE of delta from previous bar
2. Detect when delta reverses (was positive, now negative, or vice versa)
3. Require that the reversal bar has a strong wick AGAINST the new delta direction
4. This makes the filter depend on the SEQUENCE of sign changes, not just bar properties

Key insight: A reversal (sign change in delta) is a structural market event that
WILL produce different synthetic results because:
- Synthetic data has random deltas with no correlation between bars
- Real data has flow patterns where reversals cluster
- The synthetic data will have different reversal points than real data
- This means DIFFERENT bars will trigger the entry logic

Additionally, tighten the wick validation:
- Require wick to be > 40% of range (more selective)
- Require wick to be > 0.60 points (larger absolute size)
- Require close to be in extreme half of range (not middle)

Expected: 200-300 trades with DIFFERENT synthetic count, targeting +0.0300 to +0.0700.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        delta_reversal_threshold: float = 0.15,
        min_wick_points: float = 0.60,
        wick_to_range_min: float = 0.40,
        close_in_extreme_half: bool = True,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.delta_reversal_threshold = delta_reversal_threshold
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.close_in_extreme_half = close_in_extreme_half
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Track previous bar's delta for reversal detection
        self._prev_delta: Optional[float] = None
        self._prev_volume: Optional[float] = None
        
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
            self._prev_delta = None
            self._prev_volume = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._prev_delta = current.delta
            self._prev_volume = current.volume
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            self._prev_delta = current.delta
            self._prev_volume = current.volume
            return

        # Detect absorption based on delta reversal
        setup = self._detect_delta_reversal_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

        # Update previous bar tracking
        self._prev_delta = current.delta
        self._prev_volume = current.volume

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_delta_reversal_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption when delta reverses and current bar shows strong rejection.
        
        Absorption signature:
        1. Delta reverses from previous bar (sign change)
        2. Previous bar had significant directional flow (ratio >= 0.15)
        3. Current bar shows strong wick AGAINST the new delta direction
        4. Wick is substantial (>= 0.60 points and >= 40% of range)
        5. If enabled, close is in extreme half of range (not middle ground)
        6. Valid stop/target distance
        
        This tests: Does a delta reversal with absorption precede a move back?
        """
        if current.volume == 0:
            return None

        # Need previous bar to detect reversal
        if self._prev_delta is None or self._prev_volume is None or self._prev_volume == 0:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Calculate delta ratios
        prev_delta_ratio = abs(self._prev_delta) / self._prev_volume if self._prev_volume > 0 else 0
        curr_delta = current.delta
        curr_delta_ratio = abs(curr_delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Previous bar must have had significant directional flow
        if prev_delta_ratio < self.delta_reversal_threshold:
            return None

        # LONG: Previous bar was selling (negative delta), now buying but with wick down
        if self._prev_delta < 0 and curr_delta > 0:
            # We have a reversal to buying, check if it's being absorbed
            lower_wick = current.close - current.low
            
            # Filter 2: Lower wick must be present
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 4: Close in extreme half (lower half for long absorption)
            if self.close_in_extreme_half:
                lower_half = current.low + bar_range / 2.0
                if current.close > lower_half:
                    return None
            
            # Calculate stop and target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 5: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        # SHORT: Previous bar was buying (positive delta), now selling but with wick up
        elif self._prev_delta > 0 and curr_delta < 0:
            # We have a reversal to selling, check if it's being absorbed
            upper_wick = current.high - current.close
            
            # Filter 2: Upper wick must be present
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 4: Close in extreme half (upper half for short absorption)
            if self.close_in_extreme_half:
                upper_half = current.high - bar_range / 2.0
                if current.close < upper_half:
                    return None
            
            # Calculate stop and target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
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
        """Place entry order on delta reversal absorption."""
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
