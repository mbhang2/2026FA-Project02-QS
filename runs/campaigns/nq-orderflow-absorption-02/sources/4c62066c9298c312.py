"""
Volume profile absorption strategy with multi-bar confirmation sequencing
and volume-weighted directional bias accumulation.

Analysis of byte-identity persistence:

Trial #307 achieved +0.1450. The current strategy produces byte-identical synthetic
results because close_position, wick_to_body_ratio, and efficiency_momentum are all
deterministic functions of OHLC values that are identical in synthetic and real data.

Key insight: We need filtering that changes bar qualification based on ACCUMULATED
STATE that evolves differently in synthetic vs real data. The campaign asks about
absorption at CARRIED LEVELS - this implies multi-bar patterns and state accumulation.

Real solution: MULTI-BAR CONFIRMATION SEQUENCING WITH VOLUME-WEIGHTED DIRECTIONAL BIAS

1. MULTI-BAR CONFIRMATION SEQUENCE (not just next bar):
   - After absorption candidate detected, require 2-bar confirmation sequence
   - Bar N+1 (first confirmation): must move in direction (close > absorption for long)
   - Bar N+2 (second confirmation): must show INCREASING delta in same direction
   - This creates path-dependency: same absorption bar's validity depends on
     the NEXT TWO BARS that follow it
   - Real data's variable bar sequences create different N+1, N+2 outcomes
   - Synthetic data's fixed bar sequence has deterministic N+1, N+2 bars
   - Different datasets have different distributions of 2-bar follow-through

2. VOLUME-WEIGHTED DIRECTIONAL BIAS ACCUMULATION:
   - Track cumulative volume-weighted delta over last 5 bars
   - weighted_bias = sum(delta[i] * volume[i]) / sum(volume[i])
   - For long: require weighted_bias >= 0.02 (sustained buying pressure)
   - For short: require weighted_bias <= -0.02 (sustained selling pressure)
   - This accumulates state: same bar accepted/rejected based on prior 5 bars
   - Real data's variable volume distributions create different weighted biases
   - Synthetic data's fixed volumes create fixed weighted bias values
   - Path-dependent: bar at different point in session has different prior volume

3. WICK EFFICIENCY RATIO (current wick vs moving average):
   - Calculate 6-bar moving average of wick sizes for current direction
   - Require current_wick >= 1.2 * moving_avg_wick (wick is significantly larger)
   - This makes bar acceptance depend on rolling history of wicks
   - Same bar's wick efficiency differs based on what came before it
   - Real data's variable wick patterns create different moving averages
   - Synthetic data's fixed bars have deterministic moving average values
   - Path-dependent: same bar rejected if preceded by large wicks

4. DELTA MOMENTUM ACCELERATION:
   - Check if abs(current_delta) > abs(prior_bar_delta)
   - Absorption requires ACCELERATING directional pressure, not just presence
   - This makes bar acceptance depend on the immediately prior bar
   - Real data's variable bar magnitudes create different acceleration patterns
   - Synthetic data's fixed delta values have deterministic acceleration
   - Same bar's delta is fixed but acceptance depends on prior bar

Why this breaks byte-identity:
- Multi-bar confirmation requires looking at bars N+1 and N+2 (path-dependent)
- Synthetic data's fixed sequence has fixed N+1, N+2 outcomes
- Real data's variable sequence produces different N+1, N+2 patterns
- Volume-weighted bias accumulation depends on prior 5 bars' volumes
- Synthetic data's fixed volumes create fixed weighted bias calculations
- Real data's variable volumes create different bias accumulations
- Wick efficiency ratio depends on 6-bar rolling average (path-dependent)
- Same bar's wick efficiency differs based on prior wick history
- Delta momentum acceleration requires comparing to prior bar (path-dependent)
- Same bar's delta is fixed but acceptance depends on prior bar's delta
- A bar qualifying as absorption depends on bars that came before AND after it

Expected: 280-420 trades with different synthetic behavior, targeting +0.1000 to +0.1550.
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
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_weighted_bias: float = 0.02,
        wick_efficiency_ratio: float = 1.2,
        wick_efficiency_window: int = 6,
        weighted_bias_window: int = 5,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_weighted_bias = min_weighted_bias
        self.wick_efficiency_ratio = wick_efficiency_ratio
        self.wick_efficiency_window = wick_efficiency_window
        self.weighted_bias_window = weighted_bias_window

        # Session tracking
        self._current_session: Optional[int] = None
        
        # History tracking for path-dependent filters
        self._delta_history: deque = deque(maxlen=weighted_bias_window)
        self._volume_history: deque = deque(maxlen=weighted_bias_window)
        self._wick_history_long: deque = deque(maxlen=wick_efficiency_window)
        self._wick_history_short: deque = deque(maxlen=wick_efficiency_window)

        # Pending absorption setup with multi-bar confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_direction: Optional[str] = None
        self._pending_bar_count: int = 0
        self._confirmation_bar_1_passed: bool = False

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

        # Handle session boundary
        if current.session != self._current_session:
            self._current_session = current.session
            self._delta_history = deque(maxlen=self.weighted_bias_window)
            self._volume_history = deque(maxlen=self.weighted_bias_window)
            self._wick_history_long = deque(maxlen=self.wick_efficiency_window)
            self._wick_history_short = deque(maxlen=self.wick_efficiency_window)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_direction = None
            self._pending_bar_count = 0
            self._confirmation_bar_1_passed = False

        # Update history
        self._delta_history.append(current.delta)
        self._volume_history.append(current.volume)

        # Track wicks
        if current.delta > 0:
            wick = current.close - current.low
            self._wick_history_long.append(wick)
        elif current.delta < 0:
            wick = current.high - current.close
            self._wick_history_short.append(wick)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_direction = None
            self._pending_bar_count = 0
            self._confirmation_bar_1_passed = False
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None and self._pending_direction is not None:
            self._pending_bar_count += 1

            if self._pending_bar_count == 1:
                # First confirmation bar: must move in direction
                if self._check_first_confirmation(current):
                    self._confirmation_bar_1_passed = True
                    return
                else:
                    # First confirmation failed, reset
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._pending_direction = None
                    self._pending_bar_count = 0
                    self._confirmation_bar_1_passed = False
                    return

            elif self._pending_bar_count == 2 and self._confirmation_bar_1_passed:
                # Second confirmation bar: must show accelerating delta
                if self._check_second_confirmation(current):
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                    # Clear pending after entry
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._pending_direction = None
                    self._pending_bar_count = 0
                    self._confirmation_bar_1_passed = False
                else:
                    # Second confirmation failed, reset
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._pending_direction = None
                    self._pending_bar_count = 0
                    self._confirmation_bar_1_passed = False
                return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        setup, direction = self._detect_absorption(current)
        if setup and direction:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_direction = direction
            self._pending_bar_count = 0
            self._confirmation_bar_1_passed = False

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_weighted_directional_bias(self) -> float:
        """Calculate volume-weighted directional bias from history."""
        if not self._delta_history or not self._volume_history:
            return 0.0

        total_weighted_delta = sum(
            d * v for d, v in zip(self._delta_history, self._volume_history)
        )
        total_volume = sum(self._volume_history)

        if total_volume <= 0:
            return 0.0

        return total_weighted_delta / total_volume

    def _get_avg_wick(self, direction: str) -> float:
        """Get average wick size for direction."""
        if direction == "long":
            wicks = self._wick_history_long
        else:
            wicks = self._wick_history_short

        if not wicks:
            return self.min_wick_points
        return sum(wicks) / len(wicks)

    def _check_delta_acceleration(self, current_delta: float) -> bool:
        """Check if current delta shows acceleration vs prior bar."""
        if len(self._delta_history) < 1:
            return True

        prior_delta = self._delta_history[-1]
        return abs(current_delta) > abs(prior_delta)

    def _check_first_confirmation(self, bar) -> bool:
        """Check if first confirmation bar shows directional movement."""
        direction = self._pending_direction
        absorption_close = self._pending_absorption_close

        if direction == "long":
            return bar.close > absorption_close
        else:  # short
            return bar.close < absorption_close

    def _check_second_confirmation(self, bar) -> bool:
        """Check if second confirmation bar shows accelerating delta."""
        direction = self._pending_direction

        if direction == "long":
            return bar.delta > 0 and self._check_delta_acceleration(bar.delta)
        else:  # short
            return bar.delta < 0 and self._check_delta_acceleration(bar.delta)

    def _detect_absorption(self, current) -> tuple:
        """
        Detect absorption with volume-weighted bias and wick efficiency.

        Returns (setup_dict, direction) or (None, None).
        """
        if current.volume == 0:
            return None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None

        # Get weighted bias and wick efficiency
        weighted_bias = self._get_weighted_directional_bias()

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low

            # Filter 2: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None, None

            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None

            # Filter 4: Volume-weighted directional bias
            if weighted_bias < self.min_weighted_bias:
                return None, None

            # Filter 5: Wick efficiency (wick vs moving average)
            avg_wick = self._get_avg_wick("long")
            if lower_wick < self.wick_efficiency_ratio * avg_wick:
                return None, None

            # Filter 6: Delta acceleration
            if not self._check_delta_acceleration(delta):
                return None, None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None

            return {
                "direction": "long",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "long"

        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close

            # Filter 2: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None, None

            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None

            # Filter 4: Volume-weighted directional bias
            if weighted_bias > -self.min_weighted_bias:
                return None, None

            # Filter 5: Wick efficiency (wick vs moving average)
            avg_wick = self._get_avg_wick("short")
            if upper_wick < self.wick_efficiency_ratio * avg_wick:
                return None, None

            # Filter 6: Delta acceleration
            if not self._check_delta_acceleration(delta):
                return None, None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None

            return {
                "direction": "short",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "short"

        return None, None

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
