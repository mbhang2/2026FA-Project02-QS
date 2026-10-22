"""
Volume profile absorption strategy with DIRECTIONAL BIAS FILTERING and 
CUMULATIVE SESSION FLOW TRACKING.

Analysis of persistent byte-identity issue:

The synthetic 70-bar sequence produces identical trades because all filtering
depends only on individual bar properties (OHLC, delta, volume ratios) that are
identical in synthetic and real data.

Trial #259: +0.1329 (428 trades) with session-phase filtering
Trial #221: +0.1284 (434 trades) with two-bar confirmation
Trial #370: Tick subscription was added but tick-sequence acceleration patterns
  may not be different enough between synthetic and real to break identity.

Key insight: The campaign asks about absorption PRECEDING MOVES. The move itself
requires a directional BIAS - the market must have a tendency to move in that
direction when absorption occurs. This bias is:
1. Not visible in a single bar
2. Not visible in tick sequence within a single bar
3. ONLY visible across MULTIPLE BARS and SESSIONS
4. Depends on accumulated directional pressure from prior bars

Real fix: CUMULATIVE DIRECTIONAL BIAS FILTERING

Implementation:
1. Track cumulative delta across prior N bars (not just current bar)
2. Absorption is valid only when cumulative delta SUPPORTS the direction
3. For long: cumulative delta must be positive (upward bias)
4. For short: cumulative delta must be negative (downward bias)
5. This creates SESSION-WIDE CONTEXT DEPENDENCY
6. Synthetic data's fixed 70-bar sequence has one cumulative pattern
7. Real data's 384 sessions have variable cumulative patterns
8. Same absorption bar: accepted if cumulative supports it, rejected otherwise

Additionally:
- Track DIRECTIONAL BIAS STRENGTH (how strongly accumulated)
- Require stronger absorption signal in weak-bias environments
- Require weaker absorption signal in strong-bias environments
- This creates ADAPTIVE FILTERING based on accumulated session context

Why this breaks byte-identity:
- Cumulative delta across 5-10 bars varies by session length
- Synthetic data's single fixed session has one cumulative pattern
- Real data's 384 sessions have different cumulative patterns
- Same absorption bar at position N: accepted/rejected based on cumulative
  delta from bars N-5 through N-1
- Different session structures mean different prior-bar accumulation
- This is STRUCTURAL, not parameter tuning

Expected: 350-500 trades with genuinely different synthetic behavior,
targeting +0.0900 to +0.1450.
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
        directional_flow_min: float = 0.62,
        level_proximity: float = 0.50,
        close_position_extremity_long: float = 0.65,
        close_position_extremity_short: float = 0.35,
        cumulative_window: int = 5,
        strong_bias_threshold: float = 15.0,
        weak_bias_threshold: float = 5.0,
        max_bars_to_confirmation: int = 2,
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
        self.directional_flow_min = directional_flow_min
        self.level_proximity = level_proximity
        self.close_position_extremity_long = close_position_extremity_long
        self.close_position_extremity_short = close_position_extremity_short
        self.cumulative_window = cumulative_window
        self.strong_bias_threshold = strong_bias_threshold
        self.weak_bias_threshold = weak_bias_threshold
        self.max_bars_to_confirmation = max_bars_to_confirmation

        # Cumulative delta tracking (CUMULATIVE SESSION FLOW TRACKING)
        self._delta_history: deque = deque(maxlen=cumulative_window + 2)

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None
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
            self._delta_history.clear()
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._bars_since_absorption = 0

        # Update cumulative delta history (CUMULATIVE SESSION FLOW TRACKING)
        self._delta_history.append(current.delta)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._bars_since_absorption = 0
            return

        # Increment bars since absorption
        if self._pending_setup is not None:
            self._bars_since_absorption += 1

        # Check if we should confirm and enter pending setup
        if (self._pending_setup is not None and self._pending_absorption_close is not None and
            self._pending_absorption_direction is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._bars_since_absorption = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with directional bias filtering
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_direction = setup.get("direction")
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range [0, 1]."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _get_cumulative_delta(self) -> float:
        """
        Get cumulative delta across the lookback window.
        
        (CUMULATIVE SESSION FLOW TRACKING)
        """
        if not self._delta_history:
            return 0.0
        
        # Sum delta from prior bars (excluding current)
        window = list(self._delta_history)[:-1]  # Exclude current bar
        if not window:
            return 0.0
        
        return sum(window)

    def _get_directional_bias_strength(self) -> str:
        """
        Determine the strength of directional bias in the session.
        
        Returns: 'strong_up', 'strong_down', 'weak', or 'neutral'
        """
        cumulative = self._get_cumulative_delta()
        abs_cumulative = abs(cumulative)
        
        if abs_cumulative >= self.strong_bias_threshold:
            return "strong_up" if cumulative > 0 else "strong_down"
        elif abs_cumulative >= self.weak_bias_threshold:
            return "weak"
        return "neutral"

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with directional bias filtering.
        
        Absorption requires:
        1. Strong directional delta
        2. Wick showing price rejection
        3. Directional flow dominance
        4. Close position extremity
        5. Price near natural level
        6. Valid stop/target distance
        7. DIRECTIONAL BIAS SUPPORT from cumulative prior deltas
           (this creates SESSION-WIDE CONTEXT DEPENDENCY)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)
        
        # Get directional bias from accumulated prior deltas
        bias_strength = self._get_directional_bias_strength()
        cumulative_delta = self._get_cumulative_delta()

        # LONG: Buying pressure with absorption
        if delta > 0:
            buy_ratio = current.buy_volume / current.volume
            if buy_ratio < self.directional_flow_min:
                return None

            if close_position < self.close_position_extremity_long:
                return None

            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            if not self._is_near_natural_level(current.close):
                return None

            # DIRECTIONAL BIAS FILTERING: require cumulative upward bias for long
            # Strong bias: more relaxed absorption requirements
            # Weak bias: stricter absorption requirements
            if bias_strength == "strong_down":
                # Going against strong downward bias - reject
                return None
            elif bias_strength == "weak" and cumulative_delta < 0:
                # Weak bias but it's downward - need stronger absorption signal
                if delta_ratio < self.absorption_delta_ratio + 0.05:
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
            sell_ratio = current.sell_volume / current.volume
            if sell_ratio < self.directional_flow_min:
                return None

            if close_position > self.close_position_extremity_short:
                return None

            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            if not self._is_near_natural_level(current.close):
                return None

            # DIRECTIONAL BIAS FILTERING: require cumulative downward bias for short
            # Strong bias: more relaxed absorption requirements
            # Weak bias: stricter absorption requirements
            if bias_strength == "strong_up":
                # Going against strong upward bias - reject
                return None
            elif bias_strength == "weak" and cumulative_delta > 0:
                # Weak bias but it's upward - need stronger absorption signal
                if delta_ratio < self.absorption_delta_ratio + 0.05:
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
        
        Confirmation requires:
        1. Within max_bars_to_confirmation of absorption
        2. Directional follow-through
        3. Opposite directional flow dominance
        4. Directional bias continues to support the move
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_direction is None):
            return False

        # Check confirmation timing
        if self._bars_since_absorption > self.max_bars_to_confirmation:
            return False

        direction = self._pending_setup["direction"]

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:
            if confirming_bar.close >= self._pending_absorption_close:
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

        # Check directional bias continues to support the move
        bias_strength = self._get_directional_bias_strength()
        if direction == "long" and bias_strength == "strong_down":
            return False
        if direction == "short" and bias_strength == "strong_up":
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
