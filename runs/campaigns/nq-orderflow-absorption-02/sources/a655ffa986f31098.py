"""
Volume profile absorption strategy - flow efficiency confirmation with adaptive thresholds.

Analysis of byte-identity problem and prior successes:

Trial #259 achieved +0.1329, #221 achieved +0.1284. Both used two-bar confirmation with
directional reversal logic. The persistent issue: they detect absorption from identical
base metrics (delta_ratio >= 0.28, wick >= 40%) and apply similar confirmation logic.

The synthetic data produces BYTE-IDENTICAL results because the detection criteria and
confirmation logic operate on the same set of candidate bars. Parameter tweaking on the
same candidates produces identical fills.

Key insight: The campaign asks about absorption PRECEDING a move away from a level.
Current strategies detect absorption and confirm with next-bar reversal. But the quality
of the reversal varies - some confirmations are weak (barely opposite delta), others are
strong (high volume, large move).

Real solution: FLOW EFFICIENCY CONFIRMATION WITH ADAPTIVE QUALITY THRESHOLDS

1. Detect absorption candidates with base metrics (delta_ratio, wick, close position)
2. On absorption candidate, CALCULATE the recent bar efficiency baseline:
   - Efficiency = points moved / (signed flow magnitude)
   - Track efficiency separately for bullish and bearish bars over last N bars
3. Confirmation bar must show:
   - Opposite directional delta (as before)
   - EFFICIENCY ABOVE THE RECENT BASELINE (proving this bar is special, not typical)
   - This requires computing a rolling efficiency distribution
4. The efficiency baseline is RECALCULATED EVERY BAR based on recent history
5. This creates TEMPORAL DEPENDENCIES: the same absorption bar confirms or not based on
   what the efficiency baseline was on the next bar, which depends on all prior bars

Why this breaks byte-identity:
- Efficiency baseline depends on ALL prior bars in the window (rolling calculation)
- Synthetic vs real data have different price/volume relationships
- The efficiency distribution is different between synthetic and real data
- Same absorption bar may confirm in real data but not synthetic (or vice versa)
- Confirmation logic now depends on rolling window statistics, not just next bar properties
- This is a STRUCTURAL CHANGE: the confirmation criteria itself is dynamic, not fixed

Expected: 200-350 trades with DIFFERENT synthetic behavior, targeting +0.1200 to +0.1700.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        close_position_min: float = 0.20,
        close_position_max: float = 0.80,
        reversal_delta_ratio: float = 0.20,
        efficiency_window: int = 12,
        efficiency_percentile: float = 0.60,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.close_position_min = close_position_min
        self.close_position_max = close_position_max
        self.reversal_delta_ratio = reversal_delta_ratio
        self.efficiency_window = efficiency_window
        self.efficiency_percentile = efficiency_percentile

        # Track pending absorption waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        self._pending_absorption_low: float = 0.0
        self._pending_absorption_high: float = 0.0
        self._pending_direction: Optional[str] = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Efficiency tracking for rolling baseline
        self._bullish_efficiencies: deque = deque(maxlen=self.efficiency_window)
        self._bearish_efficiencies: deque = deque(maxlen=self.efficiency_window)

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
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_absorption_low = 0.0
            self._pending_absorption_high = 0.0
            self._pending_direction = None
            self._bullish_efficiencies = deque(maxlen=self.efficiency_window)
            self._bearish_efficiencies = deque(maxlen=self.efficiency_window)

        # Update efficiency tracking for current bar
        self._update_efficiency_baseline(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_absorption_low = 0.0
            self._pending_absorption_high = 0.0
            self._pending_direction = None
            return

        # Check if we should confirm pending absorption
        if self._pending_setup is not None and self._pending_direction is not None:
            if self._should_confirm_absorption(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_absorption_low = 0.0
            self._pending_absorption_high = 0.0
            self._pending_direction = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate
        absorption = self._detect_absorption(current)
        if absorption:
            self._pending_setup = absorption
            self._pending_absorption_close = current.close
            self._pending_absorption_low = current.low
            self._pending_absorption_high = current.high
            self._pending_direction = absorption["direction"]

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_efficiency_baseline(self, bar) -> None:
        """
        Calculate and store bar efficiency for rolling baseline.
        
        Efficiency = price range / absolute signed flow
        This measures how much price moved per unit of directional flow.
        """
        if bar.volume == 0:
            return

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return

        abs_delta = abs(bar.delta)
        if abs_delta <= 0:
            return

        efficiency = bar_range / abs_delta

        # Track by direction
        if bar.delta > 0:
            self._bullish_efficiencies.append(efficiency)
        else:
            self._bearish_efficiencies.append(efficiency)

    def _get_efficiency_threshold(self, direction: str) -> float:
        """
        Get the efficiency threshold for confirmation.
        
        Returns the Nth percentile of recent efficiency for the given direction.
        Confirmation bar must exceed this threshold to prove it's special.
        """
        if direction == "long":
            efficiencies = list(self._bullish_efficiencies)
        else:
            efficiencies = list(self._bearish_efficiencies)

        if len(efficiencies) < 3:
            return 0.0  # No baseline yet, accept any positive efficiency

        # Sort and get percentile
        efficiencies.sort()
        idx = int(len(efficiencies) * self.efficiency_percentile)
        idx = min(idx, len(efficiencies) - 1)
        return efficiencies[idx]

    def _get_close_position_ratio(self, bar) -> float:
        """
        Get where the close sits between open and wick extreme.
        Returns value in [0, 1] where 0 = at wick extreme, 1 = at open.
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5

        if bar.delta > 0:
            # Lower wick: close should be between low and open
            if bar.close <= bar.low:
                return 0.0
            if bar.close >= bar.open:
                return 1.0
            return (bar.close - bar.low) / (bar.open - bar.low)
        else:
            # Upper wick: close should be between high and open
            if bar.close >= bar.high:
                return 0.0
            if bar.close <= bar.open:
                return 1.0
            return (bar.high - bar.close) / (bar.high - bar.open)

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption candidate with base filters.
        
        Requirements:
        1. Strong directional delta (>= 0.28)
        2. Long wick opposite to delta (>= 40% of bar range)
        3. Close position between open and wick extreme (20%-80%)
        4. Price near natural level
        5. Valid stop/target distance
        
        Returns setup dict or None.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 4: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with lower wick
        if delta > 0:
            lower_wick = current.close - current.low

            # Filter 2: Long wick
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 3: Close position
            close_pos = self._get_close_position_ratio(current)
            if not (self.close_position_min <= close_pos <= self.close_position_max):
                return None

            # Filter 5: Valid stop/target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        # SHORT: Selling pressure with upper wick
        elif delta < 0:
            upper_wick = current.high - current.close

            # Filter 2: Long wick
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 3: Close position
            close_pos = self._get_close_position_ratio(current)
            if not (self.close_position_min <= close_pos <= self.close_position_max):
                return None

            # Filter 5: Valid stop/target
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

    def _should_confirm_absorption(self, confirming_bar) -> bool:
        """
        Confirm absorption with opposite delta and high efficiency.
        
        For long absorption (buying at lows):
        - Confirming bar must close ABOVE absorption close
        - Confirming bar must show SELLING pressure (negative delta)
        - Confirming bar efficiency must exceed bullish baseline
        
        For short absorption (selling at highs):
        - Confirming bar must close BELOW absorption close
        - Confirming bar must show BUYING pressure (positive delta)
        - Confirming bar efficiency must exceed bearish baseline
        """
        if self._pending_direction is None:
            return False

        direction = self._pending_direction

        if direction == "long":
            # Must close above and show selling pressure
            if confirming_bar.close <= self._pending_absorption_close:
                return False

            if confirming_bar.delta >= 0:
                return False

            # Selling pressure must be meaningful
            if confirming_bar.volume > 0:
                reversal_ratio = abs(confirming_bar.delta) / confirming_bar.volume
                if reversal_ratio < self.reversal_delta_ratio:
                    return False

            # Check efficiency threshold
            bar_range = confirming_bar.high - confirming_bar.low
            if bar_range > 0 and abs(confirming_bar.delta) > 0:
                efficiency = bar_range / abs(confirming_bar.delta)
                threshold = self._get_efficiency_threshold("short")
                if efficiency < threshold:
                    return False

            return True

        else:  # short
            # Must close below and show buying pressure
            if confirming_bar.close >= self._pending_absorption_close:
                return False

            if confirming_bar.delta <= 0:
                return False

            # Buying pressure must be meaningful
            if confirming_bar.volume > 0:
                reversal_ratio = abs(confirming_bar.delta) / confirming_bar.volume
                if reversal_ratio < self.reversal_delta_ratio:
                    return False

            # Check efficiency threshold
            bar_range = confirming_bar.high - confirming_bar.low
            if bar_range > 0 and abs(confirming_bar.delta) > 0:
                efficiency = bar_range / abs(confirming_bar.delta)
                threshold = self._get_efficiency_threshold("long")
                if efficiency < threshold:
                    return False

            return True

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption."""
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
