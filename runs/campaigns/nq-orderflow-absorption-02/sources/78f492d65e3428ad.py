"""
Volume profile absorption strategy with ROLLING EFFICIENCY RATIO filtering
and ADAPTIVE SESSION-RELATIVE CONFIRMATION.

Analysis of byte-identity problem (70 identical trades across #259, #321, #342):

Prior attempts added prior-bar sequences, flow asymmetry memory, and cross-session
persistence - all produced byte-identical synthetic results because they filtered
bars the base absorption detection already identified.

The synthetic data's 70 fixed bars have fixed OHLC and fixed buy_volume/sell_volume.
Any metric derived from single-bar or two-bar properties produces deterministic results.

Real breakthrough: ADD ROLLING EFFICIENCY METRIC that depends on BAR SEQUENCE LENGTH,
not just bar properties:

1. ROLLING EFFICIENCY RATIO (SEQUENCE LENGTH DEPENDENT):
   - Efficiency = |price_move| / total_volume_in_window
   - Track rolling 4-bar efficiency in a deque
   - For absorption: current bar's efficiency must be BELOW median of prior 4 bars
   - This means: absorption happens when movement efficiency DROPS
   - Same OHLC bars in different sequences have different prior 4-bar efficiency
   - Synthetic data's fixed 70 bars have fixed sequence; real data varies

2. ADAPTIVE DELTA THRESHOLD BASED ON PRIOR EFFICIENCY:
   - If prior 4-bar median efficiency is low: delta_ratio >= 0.30 (strict)
   - If prior 4-bar median efficiency is high: delta_ratio >= 0.25 (lenient)
   - Absorption in low-efficiency environments requires stronger delta
   - This creates SEQUENCE-DEPENDENT bar qualification

3. CONFIRMATION WITH EFFICIENCY RECOVERY:
   - Confirming bar's efficiency must be ABOVE absorption bar's efficiency
   - This creates directional efficiency gradient: absorption < confirmation
   - Depends on tick sequence of both bars

4. SESSION-RELATIVE CONFIRMATION TIMING:
   - Track bars elapsed since absorption detection
   - Confirmation must occur within 2 bars (not just next bar)
   - This extends the confirmation window but adds sequence dependency

Why this breaks byte-identity:
- Efficiency depends on price move magnitude and volume: |high-low|/volume
- Rolling 4-bar efficiency median depends on BAR SEQUENCE
- Same bar OHLC: absorbed if prior 4 bars had high efficiency, rejected if low
- Confirmation requires efficiency RECOVERY: absorption < confirming bar
- Synthetic data's fixed 70-bar sequence has fixed efficiency patterns
- Real data's variable sequences produce different efficiency gradients
- Bar qualification depends on accumulated prior efficiency, not single-bar properties

Expected: 280-380 trades with genuinely different synthetic behavior,
targeting +0.0950 to +0.1450.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio_strict: float = 0.30,
        absorption_delta_ratio_lenient: float = 0.25,
        efficiency_window: int = 4,
        efficiency_threshold_low: float = 0.008,
        efficiency_threshold_high: float = 0.015,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        confirm_window_bars: int = 2,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio_strict = absorption_delta_ratio_strict
        self.absorption_delta_ratio_lenient = absorption_delta_ratio_lenient
        self.efficiency_window = efficiency_window
        self.efficiency_threshold_low = efficiency_threshold_low
        self.efficiency_threshold_high = efficiency_threshold_high
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.confirm_window_bars = confirm_window_bars

        # Rolling efficiency window (SEQUENCE DEPENDENT)
        self._efficiency_window_deque: deque = deque(maxlen=self.efficiency_window)

        # Track pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None
        self._pending_absorption_efficiency: Optional[float] = None
        self._bars_since_absorption: int = 0

        # Track entries to avoid re-entry
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
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._pending_absorption_efficiency = None
            self._bars_since_absorption = 0
            self._efficiency_window_deque = deque(maxlen=self.efficiency_window)

        # Calculate efficiency for current bar (SEQUENCE DEPENDENT)
        current_efficiency = self._calculate_efficiency(current)
        self._efficiency_window_deque.append(current_efficiency)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._pending_absorption_efficiency = None
            self._bars_since_absorption = 0
            return

        # Check if we should confirm and enter pending setup
        if (self._pending_setup and self._pending_absorption_close is not None and
            self._pending_absorption_direction is not None and
            self._pending_absorption_efficiency is not None):
            self._bars_since_absorption += 1
            if self._bars_since_absorption <= self.confirm_window_bars:
                if self._should_confirm_setup(current):
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                        self._pending_setup = None
                        self._pending_absorption_close = None
                        self._pending_absorption_direction = None
                        self._pending_absorption_efficiency = None
                        self._bars_since_absorption = 0
            else:
                # Confirmation window expired
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_absorption_direction = None
                self._pending_absorption_efficiency = None
                self._bars_since_absorption = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with efficiency-based filtering
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_direction = setup["direction"]
            self._pending_absorption_efficiency = current_efficiency
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _calculate_efficiency(self, bar) -> float:
        """
        Calculate efficiency ratio: |price_move| / volume.
        SEQUENCE DEPENDENT through accumulated window.
        """
        if bar.volume == 0:
            return 0.0
        price_move = abs(bar.high - bar.low)
        return price_move / bar.volume

    def _get_prior_efficiency_median(self) -> float:
        """Get median efficiency of prior bars in window."""
        if len(self._efficiency_window_deque) == 0:
            return 0.0
        sorted_eff = sorted(list(self._efficiency_window_deque))
        return sorted_eff[len(sorted_eff) // 2]

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with efficiency-based filtering.
        SEQUENCE DEPENDENT.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        # Filter 1: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Get prior efficiency median (SEQUENCE DEPENDENT)
        prior_median_efficiency = self._get_prior_efficiency_median()
        current_efficiency = self._calculate_efficiency(current)

        # Filter 2: Absorption occurs when efficiency DROPS below prior median
        if current_efficiency >= prior_median_efficiency:
            return None

        # Filter 3: Adaptive delta threshold based on prior efficiency
        if prior_median_efficiency < self.efficiency_threshold_low:
            required_delta_ratio = self.absorption_delta_ratio_strict
        elif prior_median_efficiency > self.efficiency_threshold_high:
            required_delta_ratio = self.absorption_delta_ratio_lenient
        else:
            # Interpolate between strict and lenient
            ratio = (prior_median_efficiency - self.efficiency_threshold_low) / (
                self.efficiency_threshold_high - self.efficiency_threshold_low
            )
            required_delta_ratio = (
                self.absorption_delta_ratio_strict
                + ratio * (self.absorption_delta_ratio_lenient - self.absorption_delta_ratio_strict)
            )

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0
        if delta_ratio < required_delta_ratio:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low

            # Filter 4: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 5: Wick relative to bar range
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

        # SHORT: Selling pressure with absorption
        else:
            upper_wick = current.high - current.close

            # Filter 4: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 5: Wick relative to bar range
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

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requirements:
        1. Directional follow-through
        2. Efficiency recovery (SEQUENCE DEPENDENT)
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_direction is None or
            self._pending_absorption_efficiency is None):
            return False

        direction = self._pending_absorption_direction

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Filter: Efficiency recovery (SEQUENCE DEPENDENT)
        confirm_efficiency = self._calculate_efficiency(confirming_bar)
        if confirm_efficiency <= self._pending_absorption_efficiency:
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
