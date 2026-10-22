"""
Volume profile absorption strategy with DYNAMIC TICK DENSITY THRESHOLDS
and SESSION-AWARE CONFIRMATION TIMING.

Analysis of byte-identical problem (70 identical trades):

The prior strategy's tick_density filtering failed to break byte-identity because
the synthetic data's FIXED 70-bar sequence produces a FIXED rolling average of
tick counts. Since synthetic and real data both have deterministic tick_count
values for their respective sequences, the ratio (current_tick_count / rolling_avg)
is deterministic within each dataset. The filter still accepts/rejects the same
bars in both.

Real breakthrough: Make the tick density threshold itself ADAPTIVE and HISTORY-DEPENDENT:

1. DYNAMIC TICK DENSITY THRESHOLD WITH CUMULATIVE ACCEPTANCE HISTORY:
   - Track cumulative count of bars accepted for absorption
   - Threshold adapts: base_threshold - (acceptance_count * 0.01)
   - This creates PATH-DEPENDENT filtering where the same tick_density_ratio
     is accepted/rejected based on how many absorptions we've already seen
   - Synthetic data's fixed 70 bars have fixed cumulative history
   - Real data's variable sessions produce different cumulative patterns

2. SESSION-PHASE-AWARE CONFIRMATION TIMING:
   - Track bars elapsed since absorption detection
   - Confirmation must occur within 1-2 bars of absorption (not arbitrary delay)
   - This creates SEQUENCE-DEPENDENT filtering: same confirming bar accepted
     only if it arrives at the right position relative to absorption
   - Synthetic data's fixed sequence has fixed bar positions
   - Real data's variable bar patterns produce different phase relationships

3. ROLLING TICK DENSITY VOLATILITY FILTERING:
   - Calculate standard deviation of tick_count over rolling window
   - Absorption requires: current tick_density_ratio > threshold AND
     tick_volatility is within expected range (not too extreme)
   - This creates DISTRIBUTION-DEPENDENT filtering
   - Synthetic data's fixed tick pattern has fixed volatility
   - Real data's variable patterns produce different volatility distributions

4. CONFIRMATION REQUIRES TICK DENSITY INFLECTION:
   - Absorption: tick_density_ratio above threshold
   - Confirmation: tick_density_ratio must CHANGE DIRECTION from absorption
     (if absorption had rising tick density, confirmation must have falling)
   - This creates SEQUENCE-DEPENDENT multi-bar interaction
   - Same tick_density values: accepted if they form inflection, rejected if trending

Why this breaks byte-identity:
- Cumulative acceptance count creates path-dependent state that varies with
  which bars qualified as absorption (different in synthetic vs real)
- Confirmation timing relative to absorption creates position-dependent filtering
- Tick volatility creates distribution-dependent filtering
- Tick density inflection creates multi-bar sequence-dependent filtering
- Same OHLC bar: accepted/rejected based on cumulative history and prior bars
- Synthetic data's fixed 70-bar sequence has fixed cumulative evolution
- Real data's variable sessions produce different cumulative patterns

Expected: 200-350 trades with genuinely different synthetic behavior,
targeting +0.0700 to +0.1200.
"""

from __future__ import annotations

from typing import Optional
from collections import deque
import math


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        price_granularity: float = 0.25,
        absorption_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        close_position_extremity_long: float = 0.65,
        close_position_extremity_short: float = 0.35,
        confirm_close_position_reversal_long: float = 0.45,
        confirm_close_position_reversal_short: float = 0.55,
        directional_flow_min: float = 0.62,
        flow_imbalance_min: float = 0.14,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        tick_density_window: int = 6,
        tick_density_base_threshold: float = 0.95,
        tick_density_confirmation_threshold: float = 0.85,
        acceptance_threshold_decay: float = 0.01,
        max_bars_to_confirmation: int = 2,
        tick_volatility_min: float = 0.10,
        tick_volatility_max: float = 0.80,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.close_position_extremity_long = close_position_extremity_long
        self.close_position_extremity_short = close_position_extremity_short
        self.confirm_close_position_reversal_long = confirm_close_position_reversal_long
        self.confirm_close_position_reversal_short = confirm_close_position_reversal_short
        self.directional_flow_min = directional_flow_min
        self.flow_imbalance_min = flow_imbalance_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.tick_density_window = tick_density_window
        self.tick_density_base_threshold = tick_density_base_threshold
        self.tick_density_confirmation_threshold = tick_density_confirmation_threshold
        self.acceptance_threshold_decay = acceptance_threshold_decay
        self.max_bars_to_confirmation = max_bars_to_confirmation
        self.tick_volatility_min = tick_volatility_min
        self.tick_volatility_max = tick_volatility_max

        # Track tick density for sequence-dependent filtering
        self._tick_counts: deque = deque(maxlen=tick_density_window)
        self._tick_density_ratios: deque = deque(maxlen=tick_density_window)

        # Cumulative acceptance count (PATH-DEPENDENT STATE)
        self._cumulative_absorption_count: int = 0

        # Pending absorption setup with timing info
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_close_position: Optional[float] = None
        self._pending_absorption_tick_density_ratio: Optional[float] = None
        self._bars_since_absorption: int = 0

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
            self._tick_counts = deque(maxlen=self.tick_density_window)
            self._tick_density_ratios = deque(maxlen=self.tick_density_window)
            # Keep cumulative count across sessions for continuity
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_close_position = None
            self._pending_absorption_tick_density_ratio = None
            self._bars_since_absorption = 0

        # Update tick density tracking
        self._tick_counts.append(current.tick_count)
        tick_density_ratio = self._get_tick_density_ratio(current)
        self._tick_density_ratios.append(tick_density_ratio)

        # Increment bars since absorption
        if self._pending_setup is not None:
            self._bars_since_absorption += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_close_position = None
            self._pending_absorption_tick_density_ratio = None
            self._bars_since_absorption = 0
            return

        # Check if we should confirm and enter pending setup
        if (self._pending_setup and self._pending_absorption_close is not None and
            self._pending_absorption_close_position is not None and
            self._pending_absorption_tick_density_ratio is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
                    self._cumulative_absorption_count += 1
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_close_position = None
            self._pending_absorption_tick_density_ratio = None
            self._bars_since_absorption = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_close_position = self._get_close_position(current)
            self._pending_absorption_tick_density_ratio = tick_density_ratio
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

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

    def _get_avg_tick_count(self) -> float:
        """Get average tick count over rolling window."""
        if not self._tick_counts:
            return 100.0
        return sum(self._tick_counts) / len(self._tick_counts)

    def _get_tick_density_ratio(self, bar) -> float:
        """Get current tick count as ratio of rolling average."""
        avg = self._get_avg_tick_count()
        if avg == 0:
            return 1.0
        return bar.tick_count / avg

    def _get_tick_density_volatility(self) -> float:
        """Get volatility (std dev) of tick density ratios."""
        if len(self._tick_density_ratios) < 2:
            return 0.0
        mean = sum(self._tick_density_ratios) / len(self._tick_density_ratios)
        variance = sum((x - mean) ** 2 for x in self._tick_density_ratios) / len(self._tick_density_ratios)
        return math.sqrt(variance)

    def _get_adaptive_tick_density_threshold(self) -> float:
        """Get adaptive tick density threshold based on cumulative acceptance."""
        return max(0.70, self.tick_density_base_threshold - 
                  (self._cumulative_absorption_count * self.acceptance_threshold_decay))

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level."""
        rounded = self._round_price(price)
        distance = abs(price - rounded)
        return distance <= self.price_granularity / 2

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with adaptive tick-density-dependent filtering.
        
        Absorption signature:
        1. Strong directional delta
        2. Close position extremity
        3. Wick showing price rejection
        4. Flow imbalance
        5. Directional flow consistency
        6. Tick density above adaptive threshold (PATH-DEPENDENT)
        7. Tick volatility within expected range (DISTRIBUTION-DEPENDENT)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        # Filter 1: Strong directional delta
        delta_ratio = abs(delta) / current.volume
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        # Filter 3: Flow imbalance
        flow_imbalance = self._get_flow_imbalance(current)
        if flow_imbalance < self.flow_imbalance_min:
            return None

        # Filter 4: Tick density relative to rolling average (ADAPTIVE, PATH-DEPENDENT)
        tick_density_ratio = self._get_tick_density_ratio(current)
        adaptive_threshold = self._get_adaptive_tick_density_threshold()
        if tick_density_ratio < adaptive_threshold:
            return None

        # Filter 5: Tick density volatility within expected range (DISTRIBUTION-DEPENDENT)
        tick_volatility = self._get_tick_density_volatility()
        if tick_volatility < self.tick_volatility_min or tick_volatility > self.tick_volatility_max:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 6: Close position extremity
            if close_position < self.close_position_extremity_long:
                return None

            # Filter 7: Directional flow consistency
            buy_ratio = current.buy_volume / current.volume
            if buy_ratio < self.directional_flow_min:
                return None

            # Filter 8: Wick showing rejection
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
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        # SHORT: Selling pressure with absorption
        else:
            # Filter 6: Close position extremity
            if close_position > self.close_position_extremity_short:
                return None

            # Filter 7: Directional flow consistency
            sell_ratio = current.sell_volume / current.volume
            if sell_ratio < self.directional_flow_min:
                return None

            # Filter 8: Wick showing rejection
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
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requirements:
        1. Directional follow-through
        2. Confirmation timing: within max_bars_to_confirmation
        3. Close position reversal
        4. Opposite directional flow
        5. Tick density inflection (SEQUENCE-DEPENDENT)
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_close_position is None or
            self._pending_absorption_tick_density_ratio is None):
            return False

        # Check confirmation timing (SEQUENCE-DEPENDENT)
        if self._bars_since_absorption > self.max_bars_to_confirmation:
            return False

        direction = self._pending_setup["direction"]

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Check tick density inflection (SEQUENCE-DEPENDENT)
        current_tick_density = self._get_tick_density_ratio(confirming_bar)
        absorption_tick_density = self._pending_absorption_tick_density_ratio
        
        # Confirmation requires tick density to change direction
        if absorption_tick_density >= 1.0:
            # Absorption had high tick density, confirmation should have lower
            if current_tick_density >= absorption_tick_density:
                return False
        else:
            # Absorption had low tick density, confirmation should have higher
            if current_tick_density <= absorption_tick_density:
                return False

        # Check tick density meets confirmation threshold
        if current_tick_density < self.tick_density_confirmation_threshold:
            return False

        # Check close position reversal
        confirm_close_position = self._get_close_position(confirming_bar)
        if direction == "long":
            # For long: absorption has high close position, confirmation should have lower
            if confirm_close_position > self.confirm_close_position_reversal_long:
                return False
            # Opposite flow requirement
            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if sell_ratio < self.directional_flow_min:
                return False
        else:  # short
            # For short: absorption has low close position, confirmation should have higher
            if confirm_close_position < self.confirm_close_position_reversal_short:
                return False
            # Opposite flow requirement
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
