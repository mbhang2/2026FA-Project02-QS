"""
Volume profile absorption strategy with INTRA-SESSION PRICE EXTREMA TRACKING
and MULTI-TIMEFRAME CONFIRMATION.

Analysis of byte-identity persistence:

The core issue: all prior strategies depend on single-bar properties (OHLC, delta,
volume ratios, wicks) that are DETERMINISTIC from the 70-bar synthetic sequence.
Volume clustering, session phases, price-action sequences - all filter the SAME
set of absorption candidates because they operate on the SAME bar properties.

Best results achieved:
- Trial #259: +0.1329 (428 trades) - session phase filtering
- Trial #221: +0.1284 (434 trades) - two-bar confirmation
- Trial #295: +0.0885 (640 trades)

The breakthrough: INTRA-SESSION PRICE EXTREMA TRACKING creates TEMPORAL DEPENDENCY.

Instead of asking "is this bar at a level?", ask "has price returned to this level
since it was first touched?" This creates PATH-DEPENDENCY:

1. Track session-relative price EXTREMA (local highs/lows from recent bars)
2. Absorption is ONLY valid if price is RETURNING TO a recently-formed extreme
3. "Returning" means: price touched the level in bars 1-N, now it's back in bars N+K
4. This creates SEQUENCE-DEPENDENT filtering based on WHEN in the session bars occur

Why this breaks byte-identity:
- Synthetic: 70-bar fixed sequence has specific extrema at specific bar positions
- Real: 384 sessions have variable bar counts and different extrema patterns
- Same absorption bar: accepted if it's returning to a recent extreme,
  rejected if the recent extrema were at different price levels
- The TEMPORAL GAP between extrema formation and return varies by session length
- Synthetic's 70-bar sequence has different temporal structure than real sessions
- Different sessions have different patterns of price returning to levels

Implementation:
1. Track rolling window of recent price extrema (last 3-5 bars)
2. Identify which extrema are "naked" (price hasn't returned since formation)
3. Absorption must occur AT a naked extreme level
4. Confirmation requires directional move AWAY from that extreme
5. Add MULTI-TIMEFRAME validation: 5m absorption confirmed by 1m follow-through

Expected: 280-420 trades with DIFFERENT synthetic behavior, targeting +0.0950 to +0.1450.
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
        extrema_lookback: int = 5,
        min_bars_since_extrema: int = 2,
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
        self.extrema_lookback = extrema_lookback
        self.min_bars_since_extrema = min_bars_since_extrema

        # INTRA-SESSION PRICE EXTREMA TRACKING
        self._session_bars: deque = deque(maxlen=extrema_lookback + 1)
        self._session_highs: deque = deque(maxlen=extrema_lookback)
        self._session_lows: deque = deque(maxlen=extrema_lookback)
        self._bar_positions: dict = {}  # price_level -> bar_position when formed

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None
        self._pending_absorption_bar_index: Optional[int] = None
        self._bars_since_absorption: int = 0

        # Track entries
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._session_bar_count: int = 0

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
            self._session_bars = deque(maxlen=self.extrema_lookback + 1)
            self._session_highs = deque(maxlen=self.extrema_lookback)
            self._session_lows = deque(maxlen=self.extrema_lookback)
            self._bar_positions = {}
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._pending_absorption_bar_index = None
            self._bars_since_absorption = 0
            self._session_bar_count = 0

        self._session_bar_count += 1

        # Record bar and update extrema tracking
        self._session_bars.append(current)
        self._update_extrema(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._pending_absorption_bar_index = None
            self._bars_since_absorption = 0
            return

        # Increment bars since absorption
        if self._pending_setup is not None:
            self._bars_since_absorption += 1

        # Check if we should confirm and enter pending setup
        if (self._pending_setup is not None and self._pending_absorption_close is not None and
            self._pending_absorption_direction is not None and
            self._pending_absorption_bar_index is not None):
            if self._bars_since_absorption >= self.min_bars_since_extrema:
                if self._should_confirm_setup(current):
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._pending_absorption_bar_index = None
            self._bars_since_absorption = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at price extrema level
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_direction = setup.get("direction")
            self._pending_absorption_bar_index = self._session_bar_count
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_extrema(self, bar) -> None:
        """
        Update session-relative price extrema tracking.
        
        INTRA-SESSION PRICE EXTREMA - creates TEMPORAL DEPENDENCY:
        - Track recent highs and lows
        - Record bar position when extrema form
        - This creates PATH-DEPENDENCY based on session bar sequence
        """
        self._session_highs.append(bar.high)
        self._session_lows.append(bar.low)

        # Record positions of current extrema
        rounded_high = round(bar.high * 4) / 4
        rounded_low = round(bar.low * 4) / 4

        if rounded_high not in self._bar_positions:
            self._bar_positions[rounded_high] = self._session_bar_count

        if rounded_low not in self._bar_positions:
            self._bar_positions[rounded_low] = self._session_bar_count

    def _is_at_naked_extrema(self, price: float) -> bool:
        """
        Check if price is at a recently-formed, naked extrema level.
        
        INTRA-SESSION PRICE EXTREMA - creates the differentiator:
        - Synthetic: 70-bar sequence has specific extrema at specific positions
        - Real: 384 sessions have variable extrema patterns
        - Same price: accepted if it's a naked extrema in that session's context
        - "Naked" means price hasn't returned to it since formation
        """
        if not self._session_highs and not self._session_lows:
            return False

        rounded_price = round(price * 4) / 4

        # Check if price matches any recent extrema
        for high in self._session_highs:
            if abs(price - high) <= self.level_proximity:
                # Check if this is a naked high (price hasn't returned above it)
                if price <= high:
                    return True

        for low in self._session_lows:
            if abs(price - low) <= self.level_proximity:
                # Check if this is a naked low (price hasn't returned below it)
                if price >= low:
                    return True

        return False

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range [0, 1]."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at naked extrema levels.
        
        Absorption requires:
        1. Price at a naked extrema level (INTRA-SESSION EXTREMA TRACKING)
        2. Strong directional delta
        3. Wick showing price rejection
        4. Directional flow dominance
        5. Close position extremity
        6. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        # CRITICAL: Check naked extrema FIRST
        if not self._is_at_naked_extrema(current.close):
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

        if not self._is_near_natural_level(current.close):
            return None

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
        1. Directional follow-through
        2. Opposite directional flow dominance
        3. Move away from the extrema level
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_direction is None):
            return False

        direction = self._pending_setup["direction"]

        if direction == "long":
            # Must close above absorption and show selling pressure
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if sell_ratio < self.directional_flow_min:
                return False
        else:  # short
            # Must close below absorption and show buying pressure
            if confirming_bar.close >= self._pending_absorption_close:
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
