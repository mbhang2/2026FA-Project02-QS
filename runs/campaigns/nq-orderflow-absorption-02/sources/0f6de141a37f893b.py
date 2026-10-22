"""
Volume profile absorption strategy - test pure absorption without level carryover.

The synthetic identical trades across 40+ attempts suggests the issue is FUNDAMENTAL.
Every change we've made has been to the DETECTION logic, but the identical trades
persist. This means either:

1. The detection never fires and we have a fallback we don't see
2. The detection fires and selects the same bars as baseline
3. The level carryover system is the problem - maybe it's selecting baseline bars

Key insight: Trial #16 scored -0.1076 with 463 trades. That's our only positive result.
What if the level carryover is TOO SELECTIVE and is matching baseline momentum bars?

New approach: Test whether absorption exists AT ALL by:
1. **Remove all level carryover** - trade absorption anywhere, not just at levels
2. **Keep pure absorption markers** - delta_ratio, wick, counter-trend close
3. **Efficiency filter** - failed extension (low range/delta ratio)
4. **Simplify parameters** - fewer tuning knobs, clearer signal

If this produces different trades than the current strategy, we know:
- Absorption detection works
- Level carryover was matching baseline
- We need to refine level selection, not absorption detection

If this STILL produces identical trades, we know:
- Either the baseline strategy also detects absorption without levels
- Or there's something fundamentally wrong with how bars are selected

This is the diagnostic run to separate "absorption doesn't exist" from
"our level system is matching baseline."
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.45,
        min_wick_ratio: float = 0.35,
        max_efficiency_ratio: float = 0.60,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_ratio = min_wick_ratio
        self.max_efficiency_ratio = max_efficiency_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Track bars we've entered on
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

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption - NO level requirement, pure signal
        absorption = self._detect_absorption(current)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect genuine absorption: strong one-sided flow that fails to extend.
        
        Three markers (no level requirement):
        1. Strong delta (>= 0.45 of volume) - genuine one-sided flow
        2. Large wick opposite to flow (>= 0.35 of range) - failed extension
        3. Close against delta direction - counter-trend close
        4. Low efficiency (range/delta <= 0.60) - inefficient price movement
        
        This tests whether absorption exists without the level carryover system.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Marker 1: Strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Marker 2 & 3: Large wick opposite to flow + close against delta
        has_absorption = False
        if delta > 0:  # Buying pressure
            # Need large lower wick (failed extension upward)
            lower_wick = current.close - current.low
            if lower_wick / range_size >= self.min_wick_ratio:
                # Also require close to be in lower half (against momentum)
                mid = (current.high + current.low) / 2
                if current.close < mid:
                    has_absorption = True
        elif delta < 0:  # Selling pressure
            # Need large upper wick (failed extension downward)
            upper_wick = current.high - current.close
            if upper_wick / range_size >= self.min_wick_ratio:
                # Also require close to be in upper half (against momentum)
                mid = (current.high + current.low) / 2
                if current.close > mid:
                    has_absorption = True

        if not has_absorption:
            return None

        # Marker 4: Low efficiency (failed to move far relative to flow)
        if abs(delta) > 0:
            efficiency = range_size / abs(delta)
            if efficiency > self.max_efficiency_ratio:
                return None

        # Calculate stop and target
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # Short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

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
