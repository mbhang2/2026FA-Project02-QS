"""
Volume profile absorption strategy - test if synthetic identical trades are due to
the baseline itself being absorption-based, or if we need fundamentally different logic.

The synthetic test produces 70 trades that are BYTE-IDENTICAL to the current strategy
across multiple parameter changes. This is a strong signal that either:

1. The synthetic baseline IS absorption-based (and we're matching it by accident)
2. Our absorption detection is mathematically equivalent to baseline momentum
3. The synthetic data lacks the characteristics needed for absorption to fire

Key insight from trial history:
- #16 scored -0.1076 with 463 trades (pure absorption, no initiation)
- #13 scored -0.9744 with 102,546 trades (had momentum fallback)
- All others either didn't score or were under the 50% exposure floor

The fact that #16 had a real score suggests absorption CAN work on real data.
The synthetic identical trades suggest we're not implementing it correctly there.

New approach: Instead of trying to match the campaign's absorption definition exactly,
let's test a simpler hypothesis: what if we just trade bars with strong delta that
show a wick opposite to flow, WITHOUT requiring a carried level at all?

This removes the level carryover complexity that may be:
1. Not accumulating correctly
2. Not being carried forward properly
3. Preventing legitimate absorption signals

Changes:
1. **Remove level carryover requirement** - trade any strong delta + wick pattern
2. **Keep absorption detection simple** - delta_ratio 0.35, wick_ratio 0.15
3. **Keep structural stop** - just beyond bar extreme
4. **Use bar.delta directly** - the actual order flow on each bar
5. **Test on real data** - if this scores differently, level requirement was the issue

If this produces DIFFERENT trades on synthetic data, we've found the blocker.
If still identical, the synthetic baseline is likely also delta+wick based.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        min_wick_ratio: float = 0.15,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

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

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption: strong delta with wick opposite to flow
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
        Detect absorption: strong one-sided flow with failed extension.
        
        Two markers only (no level requirement):
        1. Strong delta (>= 35% of volume one-sided)
        2. Wick opposite to flow direction (>= 15% of bar range)
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

        # Marker 2: Failed extension (wick opposite to flow)
        has_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick / range_size >= self.min_wick_ratio:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick / range_size >= self.min_wick_ratio:
                has_wick = True

        if not has_wick:
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
