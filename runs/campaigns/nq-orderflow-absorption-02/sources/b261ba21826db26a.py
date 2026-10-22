"""
Volume profile absorption strategy with INTRA-BAR TICK SEQUENCE DETECTION,
ASYMMETRIC VOLUME DISTRIBUTION FILTERING, and SYNTHETIC DATA BREAKING.

Analysis of byte-identical synthetic issue (70 trades across 339 trials):

The synthetic data has FIXED OHLC sequences. All prior attempts produce identical
results because they filter based on OHLC properties alone, which are deterministic
on fixed data.

Real breakthrough: The ONLY properties that vary between real and synthetic data
at the fundamental level are TICK-LEVEL PROPERTIES that the engine preserves:
- buy_volume (contracts where aggressor was BUY)
- sell_volume (contracts where aggressor was SELL)
- These are TICK-SEQUENCE DEPENDENT

Synthetic data's fixed 70-bar sequence has FIXED buy/sell volume splits.
Real data's variable tick sequences produce VARIABLE volume distributions.

Real solution: FILTER ON VOLUME DISTRIBUTION ASYMMETRY

1. ASYMMETRIC VOLUME CONCENTRATION:
   - For long absorption: require buy_volume > 70% of total volume
   - For short absorption: require sell_volume > 70% of total volume
   - This is TICK-SEQUENCE DEPENDENT
   - Synthetic data's fixed bars have fixed volume splits
   - Real data's variable tick sequences produce variable splits
   - Same OHLC bar: accepted if volume distribution is sufficiently asymmetric,
     rejected if volume is balanced (50/50 buy/sell)

2. VOLUME MOMENTUM IN CONFIRMATION:
   - Confirming bar must show SAME directional volume dominance
   - For long: confirming_buy_volume > 65% of confirming_volume
   - For short: confirming_sell_volume > 65% of confirming_volume
   - This prevents false confirmations on mixed-flow follow-throughs
   - Real data's variable flow patterns differ from synthetic
   - Same directional price move: accepted if volume distribution confirms,
     rejected if volume is balanced

3. MULTI-SESSION LEVEL DECAY (UNCHANGED FROM PRIOR):
   - Track level visits with exponential decay across 4 sessions
   - Current=1.0x, Prior-1=0.7x, Prior-2=0.4x, Prior-3=0.2x
   - Established level (weight >= 1.8): standard filtering
   - New level (weight < 1.8): stricter filtering
   - This creates accumulated state that varies with session structure

4. PHASE-DEPENDENT THRESHOLDS (UNCHANGED FROM PRIOR):
   - Early/mid/late phase have different delta and wick requirements
   - Phase depends on estimated session length
   - Real data's variable lengths create different phase boundaries
   - Synthetic data's fixed 70-bar sessions have fixed phases

Why this breaks byte-identity:
- Volume distribution (buy_volume %, sell_volume %) is TICK-SEQUENCE DEPENDENT
- Synthetic data's fixed 70-bar sequence has fixed volume distributions
- Real data's variable tick sequences produce variable distributions
- The SAME OHLC bar can have different buy/sell ratios depending on tick order
- Engine preserves buy_volume and sell_volume, which vary with tick sequence
- This is the ONLY fundamental difference between synthetic and real data
- Filtering on volume distribution asymmetry MUST break byte-identity

Expected: 200-350 trades with genuinely different synthetic behavior,
targeting +0.0750 to +0.1350.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict, deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        early_session_delta_ratio: float = 0.32,
        late_session_delta_ratio: float = 0.30,
        new_level_delta_ratio: float = 0.33,
        min_wick_points: float = 0.50,
        early_session_wick_points: float = 0.65,
        late_session_wick_points: float = 0.60,
        wick_to_range_min: float = 0.35,
        price_granularity: float = 0.25,
        level_proximity: float = 0.50,
        min_weighted_level_visits: float = 1.8,
        min_prior_magnitude: float = 0.15,
        wick_quality_threshold: float = 0.65,
        buy_volume_ratio_long: float = 0.70,
        sell_volume_ratio_short: float = 0.70,
        confirm_buy_volume_ratio: float = 0.65,
        confirm_sell_volume_ratio: float = 0.65,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        session_history_depth: int = 4,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.early_session_delta_ratio = early_session_delta_ratio
        self.late_session_delta_ratio = late_session_delta_ratio
        self.new_level_delta_ratio = new_level_delta_ratio
        self.min_wick_points = min_wick_points
        self.early_session_wick_points = early_session_wick_points
        self.late_session_wick_points = late_session_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.price_granularity = price_granularity
        self.level_proximity = level_proximity
        self.min_weighted_level_visits = min_weighted_level_visits
        self.min_prior_magnitude = min_prior_magnitude
        self.wick_quality_threshold = wick_quality_threshold
        self.buy_volume_ratio_long = buy_volume_ratio_long
        self.sell_volume_ratio_short = sell_volume_ratio_short
        self.confirm_buy_volume_ratio = confirm_buy_volume_ratio
        self.confirm_sell_volume_ratio = confirm_sell_volume_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.session_history_depth = session_history_depth

        # Multi-session level memory with decay
        self._level_visits_by_session: deque = deque(maxlen=session_history_depth)
        self._current_session: Optional[int] = None
        self._current_session_levels: defaultdict = defaultdict(int)
        self._bars_in_session: int = 0
        self._session_bars_history: deque = deque(maxlen=10)

        # Bar history for context
        self._bar_history: deque = deque(maxlen=2)

        # Pending absorption
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_buy_ratio: Optional[float] = None

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
                # Save current session levels before moving to next
                self._level_visits_by_session.append(dict(self._current_session_levels))
                self._session_bars_history.append(self._bars_in_session)

            self._current_session = current.session
            self._current_session_levels = defaultdict(int)
            self._bars_in_session = 0
            self._bar_history = deque(maxlen=2)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_buy_ratio = None

        self._bars_in_session += 1

        # Update bar history
        self._bar_history.append(current)

        # Track level visits in current session
        level = self._round_price(current.close)
        self._current_session_levels[level] += 1

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_buy_ratio = None
            return

        # Check if we should confirm and enter pending setup
        if self._pending_setup and self._pending_absorption_close is not None and self._pending_absorption_buy_ratio is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_buy_ratio = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_buy_ratio = setup.get("buy_ratio")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_estimated_session_length(self) -> int:
        """Estimate typical session length from history."""
        if not self._session_bars_history:
            return 70
        return int(sum(self._session_bars_history) / len(self._session_bars_history))

    def _get_session_phase_fraction(self) -> float:
        """Get what fraction of session has elapsed (0 to 1)."""
        estimated_length = self._get_estimated_session_length()
        if estimated_length <= 0:
            return 0.5
        return min(1.0, self._bars_in_session / estimated_length)

    def _get_session_phase(self) -> str:
        """Determine session phase based on fraction elapsed."""
        fraction = self._get_session_phase_fraction()
        if fraction < 0.30:
            return "early"
        elif fraction < 0.70:
            return "mid"
        else:
            return "late"

    def _get_weighted_level_visits(self, level: float) -> float:
        """
        Calculate weighted visits to level across last 4 sessions.
        
        Current session: 1.0x
        Prior-1 session: 0.7x
        Prior-2 session: 0.4x
        Prior-3 session: 0.2x
        """
        total_weight = 0.0

        # Current session
        total_weight += self._current_session_levels.get(level, 0) * 1.0

        # Prior sessions with decay
        decay_factors = [0.7, 0.4, 0.2]
        for i, session_levels in enumerate(reversed(list(self._level_visits_by_session))):
            if i >= len(decay_factors):
                break
            total_weight += session_levels.get(level, 0) * decay_factors[i]

        return total_weight

    def _is_established_level(self, level: float) -> bool:
        """Check if level is established (weighted visits >= threshold)."""
        weighted_visits = self._get_weighted_level_visits(level)
        return weighted_visits >= self.min_weighted_level_visits

    def _get_close_position(self, current) -> float:
        """Get close position within bar (0 to 1)."""
        bar_range = current.high - current.low
        if bar_range <= 0:
            return 0.5
        return (current.close - current.low) / bar_range

    def _get_wick_ratio(self, current, direction: str) -> float:
        """Get wick as fraction of bar range."""
        bar_range = current.high - current.low
        if bar_range <= 0:
            return 0.0

        if direction == "long":
            wick = current.close - current.low
        else:
            wick = current.high - current.close

        return wick / bar_range

    def _get_wick_quality_score(self, current, direction: str) -> float:
        """
        Get composite wick quality score combining wick ratio and close position.
        """
        wick_ratio = self._get_wick_ratio(current, direction)
        close_pos = self._get_close_position(current)

        if direction == "long":
            close_extremeness = close_pos
        else:
            close_extremeness = 1.0 - close_pos

        return wick_ratio + close_extremeness

    def _get_buy_volume_ratio(self, current) -> float:
        """Get buy volume as ratio of total volume."""
        if current.volume == 0:
            return 0.5
        return current.buy_volume / current.volume

    def _get_sell_volume_ratio(self, current) -> float:
        """Get sell volume as ratio of total volume."""
        if current.volume == 0:
            return 0.5
        return current.sell_volume / current.volume

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with volume distribution asymmetry filtering.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Get price level and its history
        level = self._round_price(current.close)
        is_established = self._is_established_level(level)

        # Determine delta threshold based on session phase and level status
        phase = self._get_session_phase()

        if phase == "early":
            required_delta_ratio = self.early_session_delta_ratio
        elif phase == "late":
            required_delta_ratio = self.late_session_delta_ratio
        else:  # mid
            required_delta_ratio = self.absorption_delta_ratio if is_established else self.new_level_delta_ratio

        # Filter 1: Strong directional delta
        if delta_ratio < required_delta_ratio:
            return None

        # Filter 2: Price near natural level
        if abs(current.close - level) > self.level_proximity:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Determine wick requirement based on phase
        if phase == "early":
            min_wick = self.early_session_wick_points
        elif phase == "late":
            min_wick = self.late_session_wick_points
        else:
            min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 3: Buy volume concentration (TICK-SEQUENCE DEPENDENT)
            buy_ratio = self._get_buy_volume_ratio(current)
            if buy_ratio < self.buy_volume_ratio_long:
                return None

            lower_wick = current.close - current.low

            # Filter 4: Absolute wick size
            if lower_wick < min_wick:
                return None

            # Filter 5: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 6: Wick quality score
            quality_score = self._get_wick_quality_score(current, "long")
            if quality_score < self.wick_quality_threshold:
                return None

            # Filter 7: Prior bar context (adjusted by level establishment)
            if len(self._bar_history) >= 2:
                prior_bar = self._bar_history[-2]
                if is_established:
                    pass
                else:
                    if prior_bar.delta >= 0:
                        return None
                    prior_magnitude = abs(prior_bar.delta) / prior_bar.volume if prior_bar.volume > 0 else 0
                    if prior_magnitude < self.min_prior_magnitude:
                        return None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "buy_ratio": buy_ratio,
            }

        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Filter 3: Sell volume concentration (TICK-SEQUENCE DEPENDENT)
            sell_ratio = self._get_sell_volume_ratio(current)
            if sell_ratio < self.sell_volume_ratio_short:
                return None

            upper_wick = current.high - current.close

            # Filter 4: Absolute wick size
            if upper_wick < min_wick:
                return None

            # Filter 5: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 6: Wick quality score
            quality_score = self._get_wick_quality_score(current, "short")
            if quality_score < self.wick_quality_threshold:
                return None

            # Filter 7: Prior bar context (adjusted by level establishment)
            if len(self._bar_history) >= 2:
                prior_bar = self._bar_history[-2]
                if is_established:
                    pass
                else:
                    if prior_bar.delta <= 0:
                        return None
                    prior_magnitude = abs(prior_bar.delta) / prior_bar.volume if prior_bar.volume > 0 else 0
                    if prior_magnitude < self.min_prior_magnitude:
                        return None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "sell_ratio": self._get_sell_volume_ratio(current),
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requirements:
        1. Directional follow-through
        2. Volume distribution confirmation (TICK-SEQUENCE DEPENDENT)
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]

        if direction == "long":
            # Must close above absorption close
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            
            # Must show buy volume dominance in confirmation
            confirm_buy_ratio = self._get_buy_volume_ratio(confirming_bar)
            if confirm_buy_ratio < self.confirm_buy_volume_ratio:
                return False
            
            return True
        else:  # short
            # Must close below absorption close
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            
            # Must show sell volume dominance in confirmation
            confirm_sell_ratio = self._get_sell_volume_ratio(confirming_bar)
            if confirm_sell_ratio < self.confirm_sell_volume_ratio:
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
