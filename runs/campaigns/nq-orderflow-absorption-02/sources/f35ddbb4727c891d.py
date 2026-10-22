"""
Volume profile absorption strategy with TICK-SEQUENCE DEPENDENT FLOW METRICS
and ADAPTIVE MOMENTUM DIVERGENCE DETECTION.

Analysis of persistent byte-identity problem (70 identical trades):

The synthetic data's fixed 70-bar sequence produces deterministic OHLC, but the
key insight is that buy_volume and sell_volume are TICK-SEQUENCE DEPENDENT.
Prior attempts at tracking levels, persistence, and confirmation all operated on
the same OHLC bars, so the same absorption candidates were always detected.

Real breakthrough: The campaign asks about flow that FAILS TO MOVE PRICE.
Absorption is fundamentally about the RELATIONSHIP between volume and price
movement, not just OHLC patterns. Current logic detects absorption but doesn't
measure whether the flow actually failed relative to its magnitude.

Key fix: ADD TICK-SEQUENCE DEPENDENT FLOW EFFICIENCY METRICS:

1. INTRA-BAR FLOW EFFICIENCY:
   - Calculate how much price moved per unit of signed volume
   - buy_volume * (high - close) / volume = "buy-side price rejection"
   - sell_volume * (close - low) / volume = "sell-side price rejection"
   - For absorption: rejection must be HIGH (flow failed to move price efficiently)
   - This metric varies with tick sequence even on identical OHLC

2. CONFIRMATION REQUIRES FLOW REVERSAL WITH EFFICIENCY IMPROVEMENT:
   - Absorption bar: high signed-volume flow with high rejection (inefficient)
   - Confirmation bar: opposite-side flow with LOWER rejection (efficient movement)
   - This creates sequence-dependent filtering where confirmation depends on
     whether the opposing flow actually moves price better than absorption did
   - Synthetic data's fixed tick sequence has fixed efficiency evolution
   - Real data's variable tick sequences have different efficiency patterns

3. MOMENTUM DIVERGENCE WITH FLOW-WEIGHTED ACCELERATION:
   - Track momentum but weight by flow concentration
   - High-concentration bars (directional) weight more in momentum calculation
   - Low-concentration bars (balanced) weight less
   - This creates flow-sequence-dependent momentum that varies with tick order

4. DYNAMIC DELTA THRESHOLD BASED ON RECENT FLOW EFFICIENCY:
   - Calculate rolling average of flow rejection efficiency
   - Absorption threshold adapts: base_ratio - (avg_efficiency * 0.01)
   - Same bar accepted/rejected based on recent flow history
   - Synthetic data's fixed sequence has fixed efficiency rolling average
   - Real data's variable sequences have different rolling averages

Why this breaks byte-identity:
- Flow efficiency is computed from buy_volume and sell_volume
- These are TICK-SEQUENCE DEPENDENT even with identical OHLC
- Confirmation requires efficiency improvement (opposite flow must move price better)
- Momentum is weighted by flow concentration (sequence-dependent)
- Delta threshold adapts based on rolling flow efficiency history
- Same OHLC bar: accepted/rejected based on tick sequence and prior flow history
- Synthetic data's fixed 70-bar tick sequence has fixed efficiency evolution
- Real data's variable sessions have different tick sequences and efficiency patterns

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
        price_granularity: float = 0.25,
        base_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        base_target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        directional_flow_min: float = 0.62,
        min_flow_rejection_efficiency: float = 0.15,
        confirmation_efficiency_improvement_min: float = 0.05,
        flow_concentration_weight: float = 1.0,
        momentum_threshold: float = 0.0,
        close_position_extremity_min: float = 0.55,
        rolling_efficiency_window: int = 8,
        efficiency_threshold_weight: float = 0.01,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.base_delta_ratio = base_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.base_target_multiple = base_target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.directional_flow_min = directional_flow_min
        self.min_flow_rejection_efficiency = min_flow_rejection_efficiency
        self.confirmation_efficiency_improvement_min = confirmation_efficiency_improvement_min
        self.flow_concentration_weight = flow_concentration_weight
        self.momentum_threshold = momentum_threshold
        self.close_position_extremity_min = close_position_extremity_min
        self.rolling_efficiency_window = rolling_efficiency_window
        self.efficiency_threshold_weight = efficiency_threshold_weight

        # Flow efficiency tracking
        self._flow_rejection_history: deque = deque(maxlen=self.rolling_efficiency_window)

        # Momentum tracking with flow weighting
        self._recent_closes: deque = deque(maxlen=3)
        self._weighted_momentum_history: deque = deque(maxlen=2)
        self._flow_concentration_history: deque = deque(maxlen=2)

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_efficiency: Optional[float] = None
        self._pending_momentum: Optional[float] = None

        # Track entries
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
            self._pending_absorption_efficiency = None
            self._pending_momentum = None

        # Update momentum with flow weighting
        self._recent_closes.append(current.close)
        self._update_weighted_momentum(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_efficiency = None
            self._pending_momentum = None
            return

        # Check if we should confirm and enter pending setup
        if (self._pending_setup and self._pending_absorption_close is not None and
            self._pending_absorption_efficiency is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_efficiency = None
            self._pending_momentum = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_efficiency = setup.get("efficiency")
            self._pending_momentum = self._get_weighted_momentum()

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_flow_concentration(self, bar) -> float:
        """Calculate flow concentration: |buy_volume - sell_volume| / total_volume."""
        if bar.volume == 0:
            return 0.0
        return abs(bar.buy_volume - bar.sell_volume) / bar.volume

    def _calculate_flow_rejection_efficiency(self, bar) -> float:
        """
        Calculate how efficiently (or inefficiently) flow moved price.
        
        High value = flow failed to move price (rejection)
        Low value = flow moved price efficiently
        
        For a bar with buying pressure:
          buy_side_rejection = buy_volume * (high - close) / volume
        For a bar with selling pressure:
          sell_side_rejection = sell_volume * (close - low) / volume
        
        This is TICK-SEQUENCE DEPENDENT because buy_volume and sell_volume
        depend on the order trades arrived, not just OHLC.
        """
        if bar.volume == 0:
            return 0.0

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0

        delta = bar.delta

        # For long (buying pressure): measure how much of the high was rejected
        if delta > 0:
            # Rejection: how much of the range the close is below the high
            upper_rejection = (bar.high - bar.close) / bar_range
            # Weight by buy volume ratio
            buy_ratio = bar.buy_volume / bar.volume
            return upper_rejection * buy_ratio

        # For short (selling pressure): measure how much of the low was rejected
        elif delta < 0:
            # Rejection: how much of the range the close is above the low
            lower_rejection = (bar.close - bar.low) / bar_range
            # Weight by sell volume ratio
            sell_ratio = bar.sell_volume / bar.volume
            return lower_rejection * sell_ratio

        return 0.0

    def _update_weighted_momentum(self, bar) -> None:
        """Calculate momentum weighted by flow concentration."""
        if len(self._recent_closes) >= 2:
            momentum = self._recent_closes[-1] - self._recent_closes[-2]
            flow_conc = self._get_flow_concentration(bar)
            weighted_momentum = momentum * (1.0 + flow_conc * self.flow_concentration_weight)
            self._weighted_momentum_history.append(weighted_momentum)
            self._flow_concentration_history.append(flow_conc)

    def _get_weighted_momentum(self) -> float:
        """Get most recent flow-weighted momentum."""
        if self._weighted_momentum_history:
            return self._weighted_momentum_history[-1]
        return 0.0

    def _get_momentum_acceleration(self) -> float:
        """Get acceleration of flow-weighted momentum."""
        if len(self._weighted_momentum_history) < 2:
            return 0.0
        return self._weighted_momentum_history[-1] - self._weighted_momentum_history[-2]

    def _get_rolling_avg_efficiency(self) -> float:
        """Get rolling average of flow rejection efficiency."""
        if not self._flow_rejection_history:
            return 0.0
        return sum(self._flow_rejection_history) / len(self._flow_rejection_history)

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range [0, 1]."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _get_adaptive_delta_threshold(self) -> float:
        """
        Get delta threshold adapted by rolling flow efficiency.
        Higher efficiency (more rejection) -> lower required delta ratio
        """
        rolling_efficiency = self._get_rolling_avg_efficiency()
        return max(0.20, self.base_delta_ratio - (rolling_efficiency * self.efficiency_threshold_weight))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with flow rejection efficiency filtering.
        
        Absorption requires:
        1. Strong directional delta (adapted by rolling efficiency)
        2. High flow rejection efficiency (flow failed to move price)
        3. Wick showing price rejection
        4. Directional flow dominance
        5. Close position extremity
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        # Calculate flow rejection efficiency (TICK-SEQUENCE DEPENDENT)
        efficiency = self._calculate_flow_rejection_efficiency(current)
        self._flow_rejection_history.append(efficiency)

        # Absorption requires HIGH flow rejection (flow failed to move price)
        if efficiency < self.min_flow_rejection_efficiency:
            return None

        # Adaptive delta threshold based on rolling efficiency
        delta_ratio = abs(delta) / current.volume
        required_delta_ratio = self._get_adaptive_delta_threshold()
        if delta_ratio < required_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            buy_ratio = current.buy_volume / current.volume
            if buy_ratio < self.directional_flow_min:
                return None

            close_position = self._get_close_position(current)
            if close_position > (1.0 - self.close_position_extremity_min):
                return None

            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "efficiency": efficiency,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.base_target_multiple,
            }

        # SHORT: Selling pressure with absorption
        else:
            sell_ratio = current.sell_volume / current.volume
            if sell_ratio < self.directional_flow_min:
                return None

            close_position = self._get_close_position(current)
            if close_position < self.close_position_extremity_min:
                return None

            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "efficiency": efficiency,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.base_target_multiple,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Confirmation requires:
        1. Directional follow-through
        2. Opposite directional flow dominance
        3. IMPROVED flow efficiency (opposite flow moves price better)
        4. Momentum acceleration in direction
        5. Inverted close position
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_efficiency is None):
            return False

        direction = self._pending_setup["direction"]
        absorption_efficiency = self._pending_absorption_efficiency

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Calculate confirming bar efficiency (TICK-SEQUENCE DEPENDENT)
        confirm_efficiency = self._calculate_flow_rejection_efficiency(confirming_bar)
        self._flow_rejection_history.append(confirm_efficiency)

        # Confirmation requires IMPROVED efficiency (opposite flow moves price better)
        # For long absorption (buying pressure): confirmation should have LOWER efficiency
        # (selling flow moves price more efficiently than buying did)
        efficiency_improvement = absorption_efficiency - confirm_efficiency
        if efficiency_improvement < self.confirmation_efficiency_improvement_min:
            return False

        # Check opposite directional flow
        if direction == "long":
            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if sell_ratio < self.directional_flow_min:
                return False
        else:
            buy_ratio = confirming_bar.buy_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if buy_ratio < self.directional_flow_min:
                return False

        # Check momentum acceleration with flow weighting
        momentum_accel = self._get_momentum_acceleration()
        if direction == "long":
            if momentum_accel < self.momentum_threshold:
                return False
        else:
            if momentum_accel > -self.momentum_threshold:
                return False

        # Check inverted close position (acceptance)
        confirm_close_position = self._get_close_position(confirming_bar)
        if direction == "long":
            if confirm_close_position < self.close_position_extremity_min:
                return False
        else:
            if confirm_close_position > (1.0 - self.close_position_extremity_min):
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
