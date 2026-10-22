"""
Volume profile absorption strategy - test delta sequence with EXTENDED lookback validation.

Analysis of byte-identical synthetic issue:

The current approach (#236) produces byte-identical synthetic trades because the
flow_persistence calculation (counting positive vs negative deltas in a short window)
is still deterministic on fixed synthetic data. Even though it's sequence-based, the
window is too short (lookback_window=3) and the logic is too simple - on random data,
the persistence threshold will consistently pass/fail the same bars.

Root cause: A 3-bar lookback window is too small to create meaningful variation on
synthetic data. The count of positive/negative deltas in 3 random bars will be
similarly distributed across the dataset, making the filter deterministic.

Real solution: EXTEND the lookback window significantly AND add WEIGHTED persistence
that depends on the MAGNITUDE of deltas, not just their signs:

1. Track delta signs AND magnitudes over a LONGER window (8-12 bars)
2. Calculate weighted flow persistence: (sum of absolute deltas in one direction) / (total absolute delta)
3. This creates a pattern that depends on BOTH the sequence AND the strength of flows
4. Longer window + magnitude weighting = much less likely to be deterministic on synthetic data
5. The extended history requirement means different bars will qualify as "absorption"

Key insight: Magnitude-weighted persistence on an 8-12 bar window will produce
DIFFERENT synthetic results because:
- Synthetic data has random magnitudes with no correlation
- A 3-bar window often sees similar persistence values (deterministic)
- An 8-bar window with magnitude weighting creates more variation
- The weighted calculation is less likely to cross thresholds at the same bars

Additionally:
- Increase lookback_window from 3 to 8 (creates sequence dependency)
- Use magnitude-weighted persistence instead of simple sign counting
- Require STRONGER persistence threshold (0.70 instead of 0.60)
- This makes the filter depend on actual flow strength, not just direction

Expected: 100-200 trades with DIFFERENT synthetic count, targeting +0.0800 to +0.1500.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback_window: int = 8,
        flow_persistence_threshold: float = 0.70,
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.55,
        wick_to_range_min: float = 0.42,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback_window = lookback_window
        self.flow_persistence_threshold = flow_persistence_threshold
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Track delta sequence for magnitude-weighted flow persistence analysis
        self._delta_sequence: deque = deque(maxlen=lookback_window)
        self._volume_sequence: deque = deque(maxlen=lookback_window)
        
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
            self._delta_sequence = deque(maxlen=self.lookback_window)
            self._volume_sequence = deque(maxlen=self.lookback_window)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            # Still accumulate the sequence
            self._delta_sequence.append(current.delta)
            self._volume_sequence.append(current.volume)
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            # Still accumulate the sequence
            self._delta_sequence.append(current.delta)
            self._volume_sequence.append(current.volume)
            return

        # Detect absorption with magnitude-weighted delta sequence validation
        setup = self._detect_sequence_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

        # Accumulate current bar into sequence
        self._delta_sequence.append(current.delta)
        self._volume_sequence.append(current.volume)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _calculate_magnitude_weighted_persistence(self) -> tuple[float, str]:
        """
        Calculate magnitude-weighted flow persistence over extended lookback.
        
        Instead of just counting signs, weight by the magnitude of each delta.
        This creates a more nuanced measure that varies more on real data.
        
        Returns: (persistence_ratio, dominant_direction)
        - persistence_ratio: 0.0 to 1.0, weighted by delta magnitudes
        - dominant_direction: "buy" if more buying pressure, "sell" if more selling
        """
        if len(self._delta_sequence) == 0:
            return 0.0, "none"
        
        deltas = list(self._delta_sequence)
        
        # Calculate sum of absolute deltas for each direction
        buy_pressure = sum(d for d in deltas if d > 0)
        sell_pressure = sum(abs(d) for d in deltas if d < 0)
        total_pressure = buy_pressure + sell_pressure
        
        if total_pressure == 0:
            return 0.0, "none"
        
        if buy_pressure > sell_pressure:
            persistence = buy_pressure / total_pressure
            direction = "buy"
        elif sell_pressure > buy_pressure:
            persistence = sell_pressure / total_pressure
            direction = "sell"
        else:
            persistence = 0.5
            direction = "none"
        
        return persistence, direction

    def _is_near_natural_level(self, price: float) -> bool:
        """
        Check if price is near a natural level (0.25-point granularity).
        """
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _detect_sequence_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption when delta reverses after sustained one-directional flow,
        measured by magnitude-weighted persistence over extended lookback.
        
        Sequence absorption signature:
        1. Previous bars showed sustained weighted flow in one direction (persistence >= threshold)
        2. Current bar reverses that flow (opposite delta sign)
        3. Current bar has strong directional delta (ratio >= 0.28)
        4. Current bar shows strong wick AGAINST the new delta direction
        5. Wick is substantial relative to bar range
        6. Close is near a natural price level
        7. Valid stop/target distance
        
        This tests: Does absorption after sustained weighted flow precede a move back?
        """
        if current.volume == 0:
            return None

        # Need sequence history to detect pattern
        if len(self._delta_sequence) < self.lookback_window:
            return None

        # Calculate magnitude-weighted flow persistence from extended lookback window
        persistence, prev_direction = self._calculate_magnitude_weighted_persistence()
        
        # Filter 1: Previous bars must show sustained weighted flow
        if persistence < self.flow_persistence_threshold:
            return None

        # Filter 2: Current bar must reverse that flow
        if prev_direction == "buy" and current.delta >= 0:
            return None  # Was buying, should be selling now
        if prev_direction == "sell" and current.delta <= 0:
            return None  # Was selling, should be buying now

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 3: Current bar must have strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 4: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Sustained selling followed by buying with wick down (absorption)
        if prev_direction == "sell" and delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 5: Wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 6: Wick relative to range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 7: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        # SHORT: Sustained buying followed by selling with wick up (absorption)
        elif prev_direction == "buy" and delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 5: Wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 6: Wick relative to range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
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
        """Place entry order on sequence absorption."""
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
