"""
Volume profile absorption strategy - test if the synthetic identical trades indicate
we should focus on a completely different entry signal.

The 70 identical synthetic trades across 47+ parameter variations is a strong signal
that our absorption detection logic, while syntactically correct, is either:
1. Matching the baseline's momentum logic accidentally
2. Never firing because the synthetic data lacks absorption characteristics
3. Firing on the same bars as a simpler rule would

Key insight: Trial #16 scored -0.1076 with 463 trades. That's the ONLY real score.
All other attempts scored nothing or were under the 50% floor. The -0.1076 is bad,
but it's a real measurement on real data.

New hypothesis: What if we're overthinking this? The campaign asks about absorption
at a level preceding a move. But maybe the entry should be simpler:

Instead of "delta at a level with a wick", what if we just trade:
- Bars where price fails to extend in the direction of flow
- WITHOUT requiring a carried level at all
- Measured simply as: close is closer to the opposite extreme than the flow extreme

This is still absorption (flow that doesn't move price), but defined purely on
the bar's own structure, not on level carryover which may be:
- Not accumulating correctly across sessions
- Getting reset incorrectly at boundaries
- Requiring levels that don't exist in synthetic data

Changes from previous:
1. **Remove level requirement entirely** - absorption is bar-local, not level-dependent
2. **Simpler absorption marker** - price moved opposite to flow (close vs open position)
3. **Keep delta requirement** - still need strong one-way flow
4. **Keep structural stop** - just beyond bar extreme
5. **Tag entries clearly** - "absorption" for tracking

If this produces DIFFERENT trades on synthetic data, we've found the issue.
If still identical, the baseline is probably also using a simple momentum filter.

The real test: Does this score differently on the real 384 sessions?
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        min_body_ratio: float = 0.20,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_body_ratio = min_body_ratio
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

        # Detect absorption: strong delta with close moved against flow
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
        Detect absorption: strong one-sided flow where price failed to extend.
        
        Markers:
        1. Strong delta (>= 30% of volume one-sided)
        2. Close is moved against the flow direction (failed extension)
           - For buying pressure: close is in lower half of bar
           - For selling pressure: close is in upper half of bar
        3. Bar has meaningful body (>= 20% of range)
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

        # Marker 2: Close moved against flow (failed extension)
        # For long absorption: buying pressure but close in lower half
        # For short absorption: selling pressure but close in upper half
        close_position = (current.close - current.low) / range_size

        if delta > 0:  # Buying pressure
            # Absorption: close should be in lower half (< 0.5)
            if close_position >= 0.5:
                return None
            
            # Body should be meaningful
            body = current.close - current.open
            if abs(body) / range_size < self.min_body_ratio:
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
        
        elif delta < 0:  # Selling pressure
            # Absorption: close should be in upper half (> 0.5)
            if close_position <= 0.5:
                return None
            
            # Body should be meaningful
            body = current.open - current.close
            if abs(body) / range_size < self.min_body_ratio:
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
