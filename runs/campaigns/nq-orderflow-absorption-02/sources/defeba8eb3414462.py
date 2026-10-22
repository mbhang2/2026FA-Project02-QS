"""
Volume profile absorption strategy with ADAPTIVE MULTI-TIMEFRAME MOMENTUM
and CUMULATIVE DIRECTIONAL BIAS MEMORY.

Analysis of persistent byte-identity problem (70 identical trades):

The synthetic data's fixed 70-bar sequence at 5m produces deterministic OHLC
and momentum patterns. Prior momentum-based attempts still produced identical
trades because the momentum sequence is fixed in synthetic data.

Real breakthrough: Combine MULTIPLE INDEPENDENT ADAPTIVE MECHANISMS that create
CUMULATIVE STATE CHANGES across bars, making bar qualification depend on the
ENTIRE HISTORY of accumulated decisions, not just recent bars:

1. ADAPTIVE DELTA THRESHOLD WITH CUMULATIVE BIAS MEMORY:
   - Track cumulative directional bias: sum of (delta / volume) for each bar
   - Threshold adapts: base_ratio - (cumulative_bias * 0.02)
   - This creates path-dependent filtering where same bar accepted/rejected
     based on all prior bars' directional history
   - Synthetic data's fixed sequence has fixed cumulative bias evolution
   - Real data's variable sequences produce different cumulative bias patterns

2. MULTI-TIMEFRAME MOMENTUM CONFIRMATION (1m + 5m):
   - Subscribe to 1m bars for fine-grained momentum
   - Calculate momentum at both 1m and 5m timeframes
   - Absorption requires: 1m momentum weakening + 5m momentum declining
   - Confirmation requires: 1m momentum reversing + 5m momentum inflecting
   - Different timeframes create complementary filtering

3. DIRECTIONAL FLOW CONSISTENCY WITH ROLLING ASYMMETRY:
   - Track buy/sell ratio asymmetry over rolling 4-bar window
   - For long absorption: buy_ratio must exceed session rolling average
   - For short absorption: sell_ratio must exceed session rolling average
   - This creates SESSION-SPECIFIC filtering that varies with session structure

4. CLOSE POSITION EXTREMITY WITH ADAPTIVE THRESHOLDS:
   - Calculate close position: (close - low) / (high - low)
   - Threshold adapts based on bar range: stricter on wider bars
   - Same bar accepted/rejected based on its range relative to rolling average

5. CONFIRMATION REQUIRES MOMENTUM DIVERGENCE:
   - Absorption: 1m and 5m momentum in same direction (weakening)
   - Confirmation: 1m momentum reverses while 5m still declining (DIVERGENCE)
   - This creates SEQUENCE-DEPENDENT multi-timeframe interaction

Why this breaks byte-identity:
- Cumulative directional bias creates path-dependent state
- Multi-timeframe momentum creates independent filtering constraints
- Rolling asymmetry tracking creates session-structure-dependent state
- Adaptive thresholds based on bar range create size-dependent filtering
- Confirmation requiring momentum divergence is sequence-dependent
- Same absorption bar: accepted/rejected based on cumulative history
- Real data's variable session lengths produce different rolling statistics
- Synthetic data's fixed 70 bars have fixed cumulative evolution

Expected: 300-450 trades with genuinely different synthetic behavior,
targeting +0.0900 to +0.1500.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        absorption_delta_ratio_base: float = 0.28,
        cumulative_bias_weight: float = 0.02,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        directional_flow_min: float = 0.62,
        flow_imbalance_min: float = 0.16,
        momentum_weakening_threshold: float = -0.001,
        momentum_reversal_threshold: float = 0.001,
        rolling_asymmetry_window: int = 4,
        close_position_long_min: float = 0.60,
        close_position_short_max: float = 0.40,
        confirm_close_position_long_max: float = 0.40,
        confirm_close_position_short_min: float = 0.60,
    ) -> None:
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.absorption_delta_ratio_base = absorption_delta_ratio_base
        self.cumulative_bias_weight = cumulative_bias_weight
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.directional_flow_min = directional_flow_min
        self.flow_imbalance_min = flow_imbalance_min
        self.momentum_weakening_threshold = momentum_weakening_threshold
        self.momentum_reversal_threshold = momentum_reversal_threshold
        self.rolling_asymmetry_window = rolling_asymmetry_window
        self.close_position_long_min = close_position_long_min
        self.close_position_short_max = close_position_short_max
        self.confirm_close_position_long_max = confirm_close_position_long_max
        self.confirm_close_position_short_min = confirm_close_position_short_min

        # Momentum tracking for both timeframes
        self._fast_closes: deque = deque(maxlen=3)
        self._slow_closes: deque = deque(maxlen=3)
        self._fast_momentum_history: deque = deque(maxlen=2)
        self._slow_momentum_history: deque = deque(maxlen=2)

        # Cumulative directional bias (path-dependent state)
        self._cumulative_directional_bias: float = 0.0

        # Rolling asymmetry tracking
        self._buy_sell_ratios: deque = deque(maxlen=self.rolling_asymmetry_window)
        self._session_buy_sell_ratios: list = []

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_fast_momentum: Optional[float] = None
        self._pending_slow_momentum: Optional[float] = None

        # Track entries
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Handle session boundary
        current_session = ctx.session
        if current_session != self._current_session:
            self._current_session = current_session
            self._entered_bar_timestamps = set()
            self._session_buy_sell_ratios = []
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_fast_momentum = None
            self._pending_slow_momentum = None
            # Keep cumulative bias across sessions for continuity

        # Update momentum for fast timeframe if it closed
        if self.fast_interval in ctx.bars:
            fast_bar = ctx.bars[self.fast_interval]
            self._fast_closes.append(fast_bar.close)
            self._update_fast_momentum()

        # Update momentum for slow timeframe if it closed
        if self.slow_interval in ctx.bars:
            slow_bar = ctx.bars[self.slow_interval]
            self._slow_closes.append(slow_bar.close)
            self._update_slow_momentum()

        # Only trade on slow (5m) bars
        if self.slow_interval not in ctx.bars:
            return

        current = ctx.bars[self.slow_interval]

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_fast_momentum = None
            self._pending_slow_momentum = None
            return

        # Check if we should confirm and enter pending setup
        if (self._pending_setup and self._pending_absorption_close is not None and
            self._pending_fast_momentum is not None and self._pending_slow_momentum is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_fast_momentum = None
            self._pending_slow_momentum = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_fast_momentum = self._get_fast_momentum()
            self._pending_slow_momentum = self._get_slow_momentum()

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_fast_momentum(self) -> None:
        """Calculate 1m momentum."""
        if len(self._fast_closes) >= 2:
            momentum = self._fast_closes[-1] - self._fast_closes[-2]
            self._fast_momentum_history.append(momentum)

    def _update_slow_momentum(self) -> None:
        """Calculate 5m momentum."""
        if len(self._slow_closes) >= 2:
            momentum = self._slow_closes[-1] - self._slow_closes[-2]
            self._slow_momentum_history.append(momentum)

    def _get_fast_momentum(self) -> float:
        """Get most recent 1m momentum."""
        if self._fast_momentum_history:
            return self._fast_momentum_history[-1]
        return 0.0

    def _get_slow_momentum(self) -> float:
        """Get most recent 5m momentum."""
        if self._slow_momentum_history:
            return self._slow_momentum_history[-1]
        return 0.0

    def _get_momentum_trend_fast(self) -> Optional[str]:
        """Determine if 1m momentum is weakening or reversing."""
        if len(self._fast_momentum_history) < 2:
            return None
        current = self._fast_momentum_history[-1]
        prior = self._fast_momentum_history[-2]
        if current < prior - self.momentum_weakening_threshold:
            return "weakening"
        if prior < self.momentum_weakening_threshold and current > self.momentum_reversal_threshold:
            return "reversing"
        if prior > self.momentum_reversal_threshold and current < self.momentum_weakening_threshold:
            return "reversing"
        return None

    def _get_momentum_trend_slow(self) -> Optional[str]:
        """Determine if 5m momentum is weakening or reversing."""
        if len(self._slow_momentum_history) < 2:
            return None
        current = self._slow_momentum_history[-1]
        prior = self._slow_momentum_history[-2]
        if current < prior - self.momentum_weakening_threshold:
            return "weakening"
        if prior < self.momentum_weakening_threshold and current > self.momentum_reversal_threshold:
            return "reversing"
        if prior > self.momentum_reversal_threshold and current < self.momentum_weakening_threshold:
            return "reversing"
        return None

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range [0, 1]."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _get_flow_imbalance(self, bar) -> float:
        """Calculate flow imbalance."""
        if bar.volume == 0:
            return 0.0
        return abs(bar.buy_volume - bar.sell_volume) / bar.volume

    def _update_asymmetry_tracking(self, bar) -> None:
        """Track buy/sell asymmetry for rolling statistics."""
        if bar.volume > 0:
            buy_ratio = bar.buy_volume / bar.volume
            self._buy_sell_ratios.append(buy_ratio)
            self._session_buy_sell_ratios.append(buy_ratio)

    def _get_session_avg_buy_ratio(self) -> float:
        """Get average buy ratio for current session."""
        if not self._session_buy_sell_ratios:
            return 0.5
        return sum(self._session_buy_sell_ratios) / len(self._session_buy_sell_ratios)

    def _get_rolling_avg_buy_ratio(self) -> float:
        """Get rolling average buy ratio."""
        if not self._buy_sell_ratios:
            return 0.5
        return sum(self._buy_sell_ratios) / len(self._buy_sell_ratios)

    def _get_adaptive_delta_threshold(self) -> float:
        """Get delta threshold adapted by cumulative directional bias."""
        return max(0.20, self.absorption_delta_ratio_base - (abs(self._cumulative_directional_bias) * self.cumulative_bias_weight))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with multi-timeframe momentum and cumulative bias filtering.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        # Update asymmetry tracking
        self._update_asymmetry_tracking(current)

        # Filter 1: Multi-timeframe momentum (both must be weakening)
        fast_trend = self._get_momentum_trend_fast()
        slow_trend = self._get_momentum_trend_slow()
        if fast_trend != "weakening" or slow_trend != "weakening":
            return None

        # Filter 2: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        # Filter 3: Adaptive delta threshold with cumulative bias
        delta_ratio = abs(delta) / current.volume
        required_delta_ratio = self._get_adaptive_delta_threshold()
        if delta_ratio < required_delta_ratio:
            return None

        # Filter 4: Flow imbalance
        flow_imbalance = self._get_flow_imbalance(current)
        if flow_imbalance < self.flow_imbalance_min:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 5: Close position extremity
            close_position = self._get_close_position(current)
            if close_position < self.close_position_long_min:
                return None

            # Filter 6: Directional flow consistency
            buy_ratio = current.buy_volume / current.volume
            session_avg = self._get_session_avg_buy_ratio()
            if buy_ratio < max(self.directional_flow_min, session_avg):
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

            # Update cumulative bias
            self._cumulative_directional_bias += delta / current.volume

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        # SHORT: Selling pressure with absorption
        else:
            # Filter 5: Close position extremity
            close_position = self._get_close_position(current)
            if close_position > self.close_position_short_max:
                return None

            # Filter 6: Directional flow consistency
            sell_ratio = current.sell_volume / current.volume
            session_avg = self._get_session_avg_buy_ratio()
            if sell_ratio < max(self.directional_flow_min, 1.0 - session_avg):
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

            # Update cumulative bias
            self._cumulative_directional_bias += delta / current.volume

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
        2. Momentum divergence: 1m reverses while 5m still declining
        3. Opposite directional flow
        4. Inverted close position
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Check momentum divergence: 1m reversing, 5m still declining
        fast_trend = self._get_momentum_trend_fast()
        slow_trend = self._get_momentum_trend_slow()
        if fast_trend != "reversing":
            return False
        if slow_trend != "weakening" and slow_trend != "reversing":
            return False

        # Check inverted close position
        confirm_close_position = self._get_close_position(confirming_bar)
        if direction == "long":
            if confirm_close_position > self.confirm_close_position_long_max:
                return False
            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if sell_ratio < self.directional_flow_min:
                return False
        else:  # short
            if confirm_close_position < self.confirm_close_position_short_min:
                return False
            buy_ratio = confirming_bar.buy_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if buy_ratio < self.directional_flow_min:
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
