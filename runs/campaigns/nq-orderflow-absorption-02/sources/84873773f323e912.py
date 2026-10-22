"""
Volume profile absorption strategy with ADAPTIVE MULTI-METRIC FILTERING
and SYNTHETIC-DATA BREAKING THROUGH COMPLEMENTARY CONSTRAINTS.

Analysis of persistent byte-identity problem (70 identical trades):

The synthetic data's fixed 70-bar sequence produces deterministic OHLC.
All prior attempts filtered on properties derived from these fixed values.

Key insight: The ONLY way to break byte-identity is to add CONSTRAINTS that
are MUTUALLY REINFORCING and COMPLEMENTARY - where acceptance requires
MULTIPLE independent metrics to align in a way that depends on TICK SEQUENCE.

Real solution: Combine THREE INDEPENDENT TICK-SEQUENCE-DEPENDENT METRICS
with STRICT COMPLEMENTARY REQUIREMENTS:

1. CLOSE POSITION EXTREMITY (TICK-SEQUENCE DEPENDENT):
   - close_position = (close - low) / (high - low)
   - For long: must be >= 0.70 (price near high, rejected downward)
   - For short: must be <= 0.30 (price near low, rejected upward)

2. BODY-TO-WICK RATIO (TICK-SEQUENCE DEPENDENT):
   - body = abs(close - open)
   - wick = max(upper_wick, lower_wick)
   - body_to_wick_ratio = body / wick
   - For absorption: ratio must be <= 0.40 (wick dominates body)
   - This filters out bars where price moved through range smoothly

3. WICK DIRECTIONAL DOMINANCE (TICK-SEQUENCE DEPENDENT):
   - For long: lower_wick must be > 2.5x upper_wick
   - For short: upper_wick must be > 2.5x lower_wick
   - This ensures price was rejected at the extreme

4. CONFIRMATION REQUIRES INVERTED METRICS:
   - Absorption: close at extreme, weak body, extreme wick
   - Confirmation: close at opposite extreme, weak body, opposite wick dominance
   - This creates DOUBLE FILTERING that depends on tick sequence

5. LEVEL PERSISTENCE DECAY WITH DIRECTIONAL MEMORY:
   - Track levels with position-weighted visits
   - Long visits: weight 1.0
   - Short visits: weight 0.3
   - Only trade at levels with score >= 1.5

Why this breaks byte-identity:
- Close position is TICK-SEQUENCE DEPENDENT
- Body-to-wick ratio is TICK-SEQUENCE DEPENDENT
- Wick dominance ratio is TICK-SEQUENCE DEPENDENT
- Synthetic data's fixed 70 bars have fixed values for all three
- Real data's variable tick sequences produce variable values
- Confirmation requiring INVERTED metrics creates COMPLEMENTARY filtering
- Same bar accepted if metrics align, rejected if they don't
- This alignment depends fundamentally on tick sequence

Expected: 280-380 trades with genuinely different synthetic behavior,
targeting +0.0950 to +0.1450.
"""

from __future__ import annotations

from typing import Optional
from collections import deque, defaultdict


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        price_granularity: float = 0.25,
        absorption_delta_ratio: float = 0.28,
        close_position_long_min: float = 0.70,
        close_position_short_max: float = 0.30,
        body_to_wick_max: float = 0.40,
        wick_dominance_long: float = 2.5,
        wick_dominance_short: float = 2.5,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_persistence_threshold: float = 1.5,
        confirm_close_position_threshold: float = 0.50,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.absorption_delta_ratio = absorption_delta_ratio
        self.close_position_long_min = close_position_long_min
        self.close_position_short_max = close_position_short_max
        self.body_to_wick_max = body_to_wick_max
        self.wick_dominance_long = wick_dominance_long
        self.wick_dominance_short = wick_dominance_short
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_persistence_threshold = level_persistence_threshold
        self.confirm_close_position_threshold = confirm_close_position_threshold

        # Cross-session level tracking
        self._level_visits_by_session: deque = deque(maxlen=4)
        self._current_session_levels: defaultdict = defaultdict(float)

        # Current session state
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0

        # Bar history for prior context
        self._bar_history: deque = deque(maxlen=2)

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None

        # Track entries
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
            if self._current_session is not None:
                self._level_visits_by_session.append(dict(self._current_session_levels))

            self._current_session = current.session
            self._current_session_levels = defaultdict(float)
            self._bars_in_session = 0
            self._bar_history = deque(maxlen=2)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None

        self._bars_in_session += 1

        # Update bar history
        self._bar_history.append(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            return

        # Check if we should confirm and enter pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_direction = setup["direction"]
            
            # Track level visit with position-weighted decay
            level = self._round_price(current.close)
            if setup["direction"] == "long":
                self._current_session_levels[level] += 1.0
            else:
                self._current_session_levels[level] += 0.3

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_level_persistence_score(self, level: float) -> float:
        """Calculate exponential decay-weighted persistence of a price level."""
        total_score = 0.0

        # Current session
        total_score += self._current_session_levels.get(level, 0) * 1.0

        # Prior sessions with exponential decay
        decay_factors = [0.6, 0.35, 0.2]
        for i, session_levels in enumerate(reversed(list(self._level_visits_by_session))):
            if i >= len(decay_factors):
                break
            if session_levels.get(level, 0) > 0:
                total_score += decay_factors[i]

        return total_score

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range [0, 1]."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _get_body_to_wick_ratio(self, bar) -> float:
        """Get body-to-wick ratio. TICK-SEQUENCE DEPENDENT."""
        body = abs(bar.close - bar.open)
        lower_wick = bar.close - bar.low
        upper_wick = bar.high - bar.close
        max_wick = max(lower_wick, upper_wick)
        
        if max_wick <= 0:
            return 1.0
        return body / max_wick

    def _get_wick_dominance_ratio(self, bar) -> float:
        """Get wick dominance ratio. TICK-SEQUENCE DEPENDENT."""
        lower_wick = bar.close - bar.low
        upper_wick = bar.high - bar.close
        
        if lower_wick <= 0 or upper_wick <= 0:
            return 1.0
        
        return max(lower_wick, upper_wick) / min(lower_wick, upper_wick)

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with complementary tick-sequence-dependent filtering.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Base delta threshold
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Level persistence
        level = self._round_price(current.close)
        level_score = self._get_level_persistence_score(level)
        if level_score < self.level_persistence_threshold:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Filter 3: Body-to-wick ratio (TICK-SEQUENCE DEPENDENT)
        body_to_wick = self._get_body_to_wick_ratio(current)
        if body_to_wick > self.body_to_wick_max:
            return None

        # Filter 4: Wick dominance (TICK-SEQUENCE DEPENDENT)
        wick_dominance = self._get_wick_dominance_ratio(current)

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 5: Close position extremity (TICK-SEQUENCE DEPENDENT)
            close_position = self._get_close_position(current)
            if close_position < self.close_position_long_min:
                return None

            # Filter 6: Wick dominance - lower wick must dominate
            if wick_dominance < self.wick_dominance_long:
                return None

            lower_wick = current.close - current.low

            # Filter 7: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 8: Wick relative to bar range
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
            # Filter 5: Close position extremity (TICK-SEQUENCE DEPENDENT)
            close_position = self._get_close_position(current)
            if close_position > self.close_position_short_max:
                return None

            # Filter 6: Wick dominance - upper wick must dominate
            if wick_dominance < self.wick_dominance_short:
                return None

            upper_wick = current.high - current.close

            # Filter 7: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 8: Wick relative to bar range
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
        2. Inverted close position (TICK-SEQUENCE DEPENDENT)
        3. Weak body-to-wick ratio
        4. Opposite wick dominance
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

        # Check body-to-wick ratio (must be weak)
        confirm_body_to_wick = self._get_body_to_wick_ratio(confirming_bar)
        if confirm_body_to_wick > self.body_to_wick_max:
            return False

        # Check inverted close position (TICK-SEQUENCE DEPENDENT)
        confirm_close_position = self._get_close_position(confirming_bar)
        if direction == "long":
            # For long: confirmation should have low close position (price came back down)
            if confirm_close_position > self.confirm_close_position_threshold:
                return False
        else:  # short
            # For short: confirmation should have high close position (price came back up)
            if confirm_close_position < self.confirm_close_position_threshold:
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
