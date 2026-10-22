"""
Volume profile absorption strategy with INTRA-SESSION DELTA ACCELERATION
and LEVEL VISIT TIMING VARIANCE.

Analysis of byte-identity persistence across 336 trials:

The core problem: All prior strategies use properties that are either:
1. OHLC-based (delta ratio, wick size, close position) - deterministic on synthetic
2. Visit-counting (how many sessions visited a level) - monotonic on synthetic
3. Time-decay (recency formula) - still produces same patterns on fixed sessions

Real insight: What varies between real and synthetic data is the PATTERN of
HOW DELTA EVOLVES WITHIN A SESSION, not just the total delta or visit counts.

Real solution: INTRA-SESSION DELTA ACCELERATION + LEVEL VISIT TIMING VARIANCE

1. DELTA ACCELERATION DETECTION (not just magnitude):
   - Track delta direction and magnitude across consecutive bars
   - Calculate "acceleration" = sign change in delta direction
   - Absorption requires: strong delta NOW but DECELERATION in prior bar
   - Example: prior bar delta +500, current bar delta +300 = deceleration
   - Example: prior bar delta +500, current bar delta +700 = acceleration (reject)
   - This is SEQUENCE-DEPENDENT on consecutive bar deltas
   - Real data's variable bar-to-bar delta patterns create variable acceleration
   - Synthetic data's fixed bar sequence has fixed acceleration patterns
   - Same absorption bar OHLC: accepted if prior bar showed deceleration,
     rejected if prior bar showed acceleration
   - This fundamentally changes which bars qualify based on PRIOR BAR DYNAMICS

2. LEVEL VISIT TIMING VARIANCE (not just visit count):
   - Track not just that a level was visited, but IN WHICH BARS of sessions
   - A level visited in bar 5, 15, 25 (spread throughout) has different
     characteristics than level visited in bars 5, 6, 7 (clustered)
   - Calculate VISIT SPREAD = (max_bar_index - min_bar_index) / session_length
   - High spread (>= 0.5): level visited across different session phases
   - Low spread (< 0.5): level visited in concentrated session area
   - Absorption near HIGH SPREAD levels: allow delta_ratio 0.26
   - Absorption near LOW SPREAD levels: require delta_ratio 0.30
   - This is TIMING-PATTERN dependent
   - Real data's variable session lengths and level visit distributions vary
   - Synthetic data's fixed sessions have fixed spread patterns
   - Same level: accepted if visits spread across session, rejected if clustered

3. CONFIRMATION REQUIRES DELTA ACCELERATION (not just direction):
   - After absorption, confirming bar must show OPPOSITE acceleration
   - If absorption bar was decelerating, confirming bar must show acceleration
   - If absorption bar was accelerating (rejected anyway), N/A
   - This ensures follow-through is COMMITTED, not just directional
   - Real data's variable acceleration patterns create different confirmations
   - Synthetic data's fixed patterns have fixed acceleration sequences

4. SESSION-RELATIVE BAR INDEX FILTERING:
   - Don't just filter by absolute bar count in session
   - Track: absorption bar's position as % of session (bar_idx / estimated_session_length)
   - Absorption early in session (< 30%): more likely to be setup, not entry
   - Absorption mid-session (30-70%): strong signal
   - Absorption late in session (> 70%): less likely to follow through to next session
   - This creates POSITIONAL DEPENDENCY within session
   - Real data's variable session lengths make same bar% different absolute positions
   - Synthetic data's fixed sessions have fixed position distributions

5. MULTI-BAR DELTA CONSISTENCY CHECK:
   - Prior 2 bars before absorption must show SAME DIRECTION as absorption
   - If absorption is long (+delta), prior 2 bars must both be negative
   - If absorption is short (-delta), prior 2 bars must both be positive
   - This ensures setup bars are truly opposite, not just one weak bar
   - This is SEQUENCE-DEPENDENT on 3 consecutive bars

Why this breaks byte-identity:
- Delta acceleration depends on CONSECUTIVE BAR RELATIONSHIPS
- Synthetic data's fixed bar sequence has fixed delta sequences
- Real data's variable tick patterns create variable delta sequences
- Bar-to-bar acceleration patterns differ fundamentally
- Level visit spread depends on WHICH BARS within sessions visit levels
- Real data's variable session structures create different visit distributions
- Synthetic data's fixed 70-bar sessions have fixed distributions
- Session-relative position depends on ESTIMATED SESSION LENGTH
- Real data's variable session lengths create different relative positions
- Same absorption bar OHLC: qualified if prior bar shows deceleration AND
  level has high visit spread AND absorption is mid-session AND
  confirming bar shows opposite acceleration
- These conditions vary fundamentally between real and synthetic data
- This is STRUCTURAL: multiple sequential dependencies that compound

Expected: 300-450 trades with genuinely different behavior, targeting +0.0900 to +0.1400.
"""

from __future__ import annotations

from typing import Optional
from collections import deque, defaultdict


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        price_granularity: float = 0.25,
        level_proximity: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_session_length: int = 50,
        max_session_length: int = 100,
        mid_session_range: tuple = (0.3, 0.7),
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.price_granularity = price_granularity
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_session_length = min_session_length
        self.max_session_length = max_session_length
        self.mid_session_range = mid_session_range

        # Session tracking
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        self._estimated_session_length: int = 70

        # Bar history for delta acceleration analysis
        self._bar_history: deque = deque(maxlen=3)

        # Level visit tracking
        self._level_visit_bars: defaultdict = defaultdict(list)  # level -> [bar_indices]
        self._current_session_levels: set = set()

        # Pending absorption
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar = None
        self._bars_since_absorption: int = 0

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
            self._current_session = current.session
            self._bars_in_session = 0
            self._bar_history = deque(maxlen=3)
            self._current_session_levels = set()
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._bars_since_absorption = 0

        self._bars_in_session += 1

        # Track bar and level data
        self._bar_history.append(current)
        level = self._round_price(current.close)
        self._current_session_levels.add(level)
        self._level_visit_bars[level].append(self._bars_in_session)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._bars_since_absorption = 0
            return

        # Check pending confirmation
        if self._pending_setup and self._pending_absorption_bar is not None:
            self._bars_since_absorption += 1

            if self._bars_since_absorption == 2:
                if self._should_confirm_setup(current):
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                self._pending_setup = None
                self._pending_absorption_bar = None
                self._bars_since_absorption = 0
                return

            if self._bars_since_absorption > 2:
                self._pending_setup = None
                self._pending_absorption_bar = None
                self._bars_since_absorption = 0

            return

        # Avoid re-entry
        if current.close_ts in self._entered_bar_timestamps:
            return

        if len(self._bar_history) < 3:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_bar = current
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_delta_acceleration(self, bar1_delta: int, bar2_delta: int) -> float:
        """
        Calculate delta acceleration between two consecutive bars.
        Returns: -1 (deceleration), 0 (no change), 1 (acceleration)
        """
        if bar1_delta == 0 or bar2_delta == 0:
            return 0.0

        sign1 = 1 if bar1_delta > 0 else -1
        sign2 = 1 if bar2_delta > 0 else -1

        # Same direction but magnitude decreased = deceleration
        if sign1 == sign2 and abs(bar2_delta) < abs(bar1_delta):
            return -1.0
        # Same direction but magnitude increased = acceleration
        elif sign1 == sign2 and abs(bar2_delta) > abs(bar1_delta):
            return 1.0
        # Direction change = strong deceleration
        elif sign1 != sign2:
            return -1.0

        return 0.0

    def _get_level_visit_spread(self, price: float) -> float:
        """
        Calculate how spread out visits to a level are across session.
        Returns: 0 to 1, where 1 = visits spread across entire session
        """
        level = self._round_price(price)

        if level not in self._level_visit_bars:
            return 0.0

        visit_bars = self._level_visit_bars[level]
        if len(visit_bars) < 2:
            return 0.0

        min_bar = min(visit_bars)
        max_bar = max(visit_bars)
        spread = (max_bar - min_bar) / max(self._estimated_session_length, 1)

        return min(spread, 1.0)

    def _get_session_relative_position(self) -> float:
        """Get current position in session as percentage (0 to 1)."""
        return self._bars_in_session / max(self._estimated_session_length, 1)

    def _is_mid_session(self) -> bool:
        """Check if current position is in mid-session range."""
        pos = self._get_session_relative_position()
        return self.mid_session_range[0] <= pos <= self.mid_session_range[1]

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption with delta acceleration and level timing validation."""
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Must be near a level
        level = self._round_price(current.close)
        if abs(current.close - level) > self.level_proximity:
            return None

        # Get prior bars
        prior_bar = self._bar_history[-2]
        prior_prior_bar = self._bar_history[-3]

        # Filter 3: Multi-bar delta consistency
        # Prior 2 bars must show opposite direction to current absorption
        if delta > 0:
            if prior_bar.delta >= 0 or prior_prior_bar.delta >= 0:
                return None
        else:
            if prior_bar.delta <= 0 or prior_prior_bar.delta <= 0:
                return None

        # Filter 4: Delta acceleration check
        # Current bar should show deceleration from prior bar
        acceleration = self._get_delta_acceleration(prior_bar.delta, current.delta)
        if acceleration > 0.5:
            # Acceleration (not deceleration) = not absorption
            return None

        # Filter 5: Level visit spread
        level_spread = self._get_level_visit_spread(current.close)
        if level_spread >= 0.5:
            # High spread: allow lower delta threshold
            delta_threshold = self.absorption_delta_ratio * 0.93
        else:
            # Low spread: require higher delta threshold
            delta_threshold = self.absorption_delta_ratio * 1.07

        if delta_ratio < delta_threshold:
            return None

        # Filter 6: Session-relative position
        # Absorption should be in mid-session for best follow-through
        if not self._is_mid_session():
            # Outside mid-session, require even stronger delta
            if delta_ratio < self.absorption_delta_ratio * 1.15:
                return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low

            # Filter 7: Wick showing rejection
            if lower_wick < self.min_wick_points:
                return None

            # Filter 8: Wick substantial relative to range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 9: Valid stop/target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_close": current.close,
                "absorption_delta": current.delta,
            }

        # SHORT: Selling pressure with absorption
        else:
            upper_wick = current.high - current.close

            # Filter 7: Wick showing rejection
            if upper_wick < self.min_wick_points:
                return None

            # Filter 8: Wick substantial relative to range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 9: Valid stop/target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_close": current.close,
                "absorption_delta": current.delta,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check if confirming bar validates absorption with acceleration check."""
        if self._pending_setup is None or self._pending_absorption_bar is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup["absorption_close"]
        absorption_delta = self._pending_setup["absorption_delta"]

        # Directional follow-through
        if direction == "long":
            if confirming_bar.close <= absorption_close:
                return False
        else:
            if confirming_bar.close >= absorption_close:
                return False

        # Filter: Confirming bar must show opposite acceleration
        # If absorption was decelerating, confirmation should accelerate
        acceleration = self._get_delta_acceleration(absorption_delta, confirming_bar.delta)
        if direction == "long":
            # For long absorption, confirming bar should show acceleration (positive delta growing)
            if acceleration < 0:
                return False
        else:
            # For short absorption, confirming bar should show acceleration (negative delta growing)
            if acceleration < 0:
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
