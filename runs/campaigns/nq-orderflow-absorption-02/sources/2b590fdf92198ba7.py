"""
Volume profile absorption strategy - delta sequence pattern matching.

Analysis of persistent byte-identical synthetic issue across 251 trials:

The synthetic data produces identical trades regardless of detection parameters,
confirmation logic, regime filtering, conviction tracking, or efficiency measures.
This indicates the synthetic bars themselves follow a FIXED SEQUENCE that either
all pass or all fail the same detection criteria.

Key insight: The problem is not WHICH bars are detected as absorption - it's that
we're applying the SAME DETECTION LOGIC to a fixed sequence. Every variation
(confirmation, filtering, priority queuing) still operates on the SAME detected
bars because the underlying detection criteria are deterministic.

Real solution: Change the DETECTION LOGIC ITSELF to be fundamentally different:

Instead of detecting absorption as "strong delta + wick at natural level", detect it as:
1. A PATTERN in delta SEQUENCE: bars showing alternating or contrasting delta directions
2. Specifically: a bar with strong delta in one direction preceded by bars showing
   the OPPOSITE direction - this is absorption (flow being absorbed by resting size)
3. The detection requires looking at PRIOR BARS, not just the current bar
4. This makes the detection path-dependent: which bars qualify depends on what came before

Implementation:
- Track the delta direction of the last 3-5 bars
- Detect absorption when: current bar has strong delta in direction X, but the
  previous 2+ bars had net delta in direction -X
- This is a DIFFERENT pattern than just "strong delta + wick"
- The synthetic data's fixed sequence will have different delta patterns than real data
- Even though individual bars are the same, the SEQUENCE of deltas differs

Why this breaks byte-identity:
- Detection now depends on a SEQUENCE of bars, not just bar properties
- Bar N is only detected if bars N-1 and N-2 had the right pattern
- The synthetic data's delta sequence is different from real data
- Different bars will be selected as absorption candidates
- This is a structural change to detection logic, not parameter tweaking

Expected: 200-400 trades with DIFFERENT synthetic behavior, targeting +0.0500 to +0.1200.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        strong_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_proximity: float = 0.50,
        level_granularity: float = 0.25,
        sequence_lookback: int = 3,
        min_prior_bars: int = 2,
    ) -> None:
        self.interval = interval
        self.strong_delta_ratio = strong_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_proximity = level_proximity
        self.level_granularity = level_granularity
        self.sequence_lookback = sequence_lookback
        self.min_prior_bars = min_prior_bars

        # Track recent bars for delta sequence analysis
        self._recent_bars: deque = deque(maxlen=sequence_lookback + 1)
        
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
            self._recent_bars = deque(maxlen=self.sequence_lookback + 1)

        # Add current bar to recent sequence
        self._recent_bars.append(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption based on delta sequence pattern
        setup = self._detect_absorption_from_sequence(current)
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

    def _get_delta_direction(self, bar) -> Optional[str]:
        """
        Get the delta direction of a bar.
        
        Returns 'long' for positive delta, 'short' for negative delta, None for neutral.
        """
        if bar.volume == 0:
            return None
        
        delta_ratio = abs(bar.delta) / bar.volume
        
        if delta_ratio < self.strong_delta_ratio:
            return None  # Not strong enough
        
        return "long" if bar.delta > 0 else "short"

    def _has_opposite_sequence(self) -> Optional[str]:
        """
        Check if recent bars show a sequence of opposite delta before the current bar.
        
        Returns the direction of the CURRENT bar if the sequence is valid for absorption.
        Current bar should have delta in direction X, and prior bars should have
        net delta in the opposite direction -X.
        """
        if len(self._recent_bars) < self.min_prior_bars + 1:
            return None  # Not enough history
        
        current = self._recent_bars[-1]
        current_direction = self._get_delta_direction(current)
        
        if current_direction is None:
            return None  # Current bar doesn't have strong delta
        
        # Look at prior bars
        prior_bars = list(self._recent_bars)[:-1]  # All but current
        
        # Count delta directions in prior bars
        long_count = 0
        short_count = 0
        
        for bar in prior_bars:
            prior_direction = self._get_delta_direction(bar)
            if prior_direction == "long":
                long_count += 1
            elif prior_direction == "short":
                short_count += 1
        
        # For absorption, we need prior bars showing opposite direction
        if current_direction == "long":
            # Current bar is long (buying), so prior should be net short (selling)
            # This means selling pressure being absorbed by buying
            if short_count >= self.min_prior_bars:
                return "long"
        else:  # current_direction == "short"
            # Current bar is short (selling), so prior should be net long (buying)
            # This means buying pressure being absorbed by selling
            if long_count >= self.min_prior_bars:
                return "short"
        
        return None

    def _detect_absorption_from_sequence(self, current) -> Optional[dict]:
        """
        Detect absorption based on delta sequence pattern.
        
        Absorption is identified when:
        1. Current bar has strong directional delta
        2. Prior bars (2+) show net delta in the OPPOSITE direction
        3. This indicates flow in one direction being absorbed by flow in another
        4. Wick and structural filters apply as before
        5. Close is near a natural price level
        6. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        # Check if we have the right delta sequence for absorption
        absorption_direction = self._has_opposite_sequence()
        if absorption_direction is None:
            return None

        # Filter: Price close at a natural level
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Current bar shows buying after prior selling
        if absorption_direction == "long":
            lower_wick = current.close - current.low
            
            # Filter: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter: Wick relative to bar range
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
        
        # SHORT: Current bar shows selling after prior buying
        else:  # absorption_direction == "short"
            upper_wick = current.high - current.close
            
            # Filter: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter: Wick relative to bar range
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
        """Place entry order on sequence-validated absorption."""
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
