"""
Volume profile absorption strategy - diagnostic attempt to understand why synthetic 
test produces identical trades.

The synthetic test reported IDENTICAL TRADES to baseline on all recent attempts 
(#5-#11), despite significant logic changes. This is highly suspicious and suggests 
one of two things:

1. The absorption detection logic is never firing at all on synthetic data
2. The synthetic data is so constrained that the baseline strategy's momentum rule 
   is the only thing that triggers

Key observation: The baseline MomentumStrategy just checks if last.close > last.open 
(buy) or last.close < last.open (sell). It trades on EVERY bar that moves, with no 
position/order checks beyond that.

Our absorption strategy requires:
- delta_ratio > 0.45 (strong one-way flow)
- visible tail (wick > 0.02) opposite to flow direction
- stop distance in [0.25, 5.0]

On pure synthetic noise, these conditions may genuinely never align, OR the synthetic 
data may have such weak/random delta that delta_ratio never exceeds 0.45.

Strategy for this attempt:
1. **Add fallback to momentum signal**: If absorption doesn't fire, fall back to 
   simple momentum (close > open = buy, close < open = sell). This ensures we 
   generate trades on synthetic data so we can see if the logic is actually working.

2. **Lower delta_ratio even further**: Try 0.35 to catch weaker directional flow

3. **Remove tail requirement**: Just use delta_ratio, no tail check. The tail 
   requirement may be the blocker on synthetic noise.

4. **Track entry reason**: Tag entries as "absorption" vs "momentum" so we can see 
   which rule is firing

5. **Keep structural stop**: The stop/target logic is correct and was never the issue

This is a diagnostic attempt. If we get identical trades again, it means the 
momentum fallback is firing (absorption never triggers). If we get different trades, 
it means we've found the blocker.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Session tracking
        self._current_session: Optional[int] = None
        
        # Track last bar index we entered on (to avoid double-entry on same bar)
        self._last_entry_bar_index: int = -1000

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

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Use history index to avoid entering twice on the same bar
        current_bar_index = len(history) - 1
        if current_bar_index == self._last_entry_bar_index:
            return

        # Try absorption signal first
        absorption = self._detect_absorption(current)
        if absorption:
            self._last_entry_bar_index = current_bar_index
            self._place_entry(ctx, absorption, "absorption")
            return

        # Fallback to simple momentum (like baseline) if no absorption
        # This ensures we generate trades on synthetic data
        if current.close > current.open:
            self._last_entry_bar_index = current_bar_index
            self._place_entry(
                ctx,
                {
                    "direction": "long",
                    "stop_distance": 0.75,
                    "target_distance": 0.75,
                },
                "momentum"
            )
        elif current.close < current.open:
            self._last_entry_bar_index = current_bar_index
            self._place_entry(
                ctx,
                {
                    "direction": "short",
                    "stop_distance": 0.75,
                    "target_distance": 0.75,
                },
                "momentum"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong one-way delta.
        
        Removed tail requirement to test if that was the blocker.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow (lowered to 0.35)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Calculate stop and target based on direction
        if delta > 0:  # Long absorption
            # Stop sits just below the absorption bar's low
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Validate stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # Short absorption (delta < 0)
            # Stop sits just above the absorption bar's high
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Validate stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        return None

    def _place_entry(self, ctx, setup: dict, reason: str) -> None:
        """Place entry with structural stop and target."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        tag = reason

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
