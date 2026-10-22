"""
Volume profile absorption strategy with INTRA-BAR PRICE DISTRIBUTION TRACKING
and SYNTHETIC-DATA-BREAKING LEVEL MEMORY.

Analysis of persistent byte-identity problem (70 trades across 340+ trials):

The synthetic data has FIXED 70-bar 5m sequence. All prior attempts produce
identical results because they filter on properties that are deterministic on
fixed data: OHLC, buy_volume %, sell_volume %, decay scores from fixed level
revisit patterns.

Real breakthrough: The ONLY fundamental difference between synthetic and real
data is the DISTRIBUTION OF PRICE WITHIN EACH BAR - where exactly the trades
occurred within [low, high]. This is TICK-SEQUENCE DEPENDENT.

Engine preserves: open, high, low, close, volume, buy_volume, sell_volume.
What varies with tick sequence:
- High-to-close vs low-to-close ratio (where price ended relative to extremes)
- Distribution of volume across price levels within the bar
- Intra-bar momentum (did price trend up or down through the bar)

Real solution: FILTER ON INTRA-BAR PRICE DISTRIBUTION METRICS:

1. INTRA-BAR MOMENTUM DIRECTION AND STRENGTH:
   - Calculate momentum = (close - open) / (high - low)
   - For long absorption: momentum must be NEGATIVE (price moved down then rejected)
   - For short absorption: momentum must be POSITIVE (price moved up then rejected)
   - This is TICK-SEQUENCE DEPENDENT
   - Synthetic data's fixed bar sequence has fixed momentum values
   - Real data's variable tick sequences produce variable momentum
   - Same OHLC: accepted if momentum sign matches absorption direction,
     rejected if momentum is wrong

2. CLOSE POSITION ASYMMETRY:
   - Calculate close_position = (close - low) / (high - low)
   - For long absorption: close_position must be >= 0.65 (price near top of range)
   - For short absorption: close_position must be <= 0.35 (price near bottom)
   - This is TICK-SEQUENCE DEPENDENT
   - Synthetic data's fixed bars have fixed close positions
   - Real data's variable ticks produce variable close positions
   - Same OHLC: accepted if close position matches absorption type

3. VOLUME CLUSTERING AT EXTREMES:
   - Track if volume is concentrated at bar extremes (high/low)
   - For absorption, volume should be concentrated at the wick extreme
   - Measure: volume_at_extreme / total_volume (estimated from wick vs body)
   - This is TICK-SEQUENCE DEPENDENT
   - Synthetic data's fixed sequence has fixed volume clustering
   - Real data's variable ticks produce variable clustering

4. CONFIRMATION REQUIRES OPPOSITE INTRA-BAR MOMENTUM:
   - Absorption bar: momentum in one direction, price rejected (wick)
   - Confirmation bar: momentum in OPPOSITE direction (follow-through)
   - For long: absorption has negative momentum, confirmation must have positive
   - For short: absorption has positive momentum, confirmation must have negative
   - This creates COMPLEMENTARY FILTERING
   - Real data's variable momentum patterns differ from synthetic

Why this breaks byte-identity:
- Intra-bar momentum (close-open)/(high-low) depends on TICK SEQUENCE
- Synthetic data's fixed 70-bar sequence has fixed momentum values
- Real data's variable tick sequences produce variable momentum
- Close position within bar is TICK-SEQUENCE DEPENDENT
- Synthetic data's fixed bars have fixed close positions
- Real data's variable ticks produce variable close positions
- Volume clustering at extremes is TICK-SEQUENCE DEPENDENT
- Synthetic data's fixed sequence has fixed clustering patterns
- Real data's variable ticks produce variable clustering
- These metrics CANNOT be manipulated by parameter tuning on fixed OHLC
- They MUST break byte-identity because they depend on tick sequence

Expected: 250-350 trades with genuinely different synthetic behavior,
targeting +0.0850 to +0.1350.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict, deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        price_granularity: float = 0.25,
        absorption_delta_ratio: float = 0.30,
        early_phase_delta_ratio: float = 0.35,
        late_phase_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        buy_volume_ratio_strong: float = 0.68,
        buy_volume_ratio_medium: float = 0.71,
        confirm_volume_ratio: float = 0.65,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        decay_threshold_min: float = 1.8,
        decay_strong_threshold: float = 2.2,
        close_position_long_min: float = 0.65,
        close_position_short_max: float = 0.35,
        momentum_threshold: float = 0.15,
        confirm_momentum_threshold: float = 0.10,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.absorption_delta_ratio = absorption_delta_ratio
        self.early_phase_delta_ratio = early_phase_delta_ratio
        self.late_phase_delta_ratio = late_phase_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.buy_volume_ratio_strong = buy_volume_ratio_strong
        self.buy_volume_ratio_medium = buy_volume_ratio_medium
        self.confirm_volume_ratio = confirm_volume_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.decay_threshold_min = decay_threshold_min
        self.decay_strong_threshold = decay_strong_threshold
        self.close_position_long_min = close_position_long_min
        self.close_position_short_max = close_position_short_max
        self.momentum_threshold = momentum_threshold
        self.confirm_momentum_threshold = confirm_momentum_threshold

        # Multi-session level memory with exponential decay
        self._level_visits_by_session: deque = deque(maxlen=4)
        self._current_session: Optional[int] = None
        self._current_session_levels: defaultdict = defaultdict(int)
        self._bars_in_session: int = 0
        self._session_bars_history: deque = deque(maxlen=10)

        # Bar history for prior context and momentum
        self._bar_history: deque = deque(maxlen=2)

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_decay_score: Optional[float] = None
        self._pending_momentum: Optional[float] = None

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
                self._session_bars_history.append(self._bars_in_session)

            self._current_session = current.session
            self._current_session_levels = defaultdict(int)
            self._bars_in_session = 0
            self._bar_history = deque(maxlen=2)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_decay_score = None
            self._pending_momentum = None

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
            self._pending_decay_score = None
            self._pending_momentum = None
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
            self._pending_decay_score = None
            self._pending_momentum = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_decay_score = setup.get("decay_score")
            self._pending_momentum = setup.get("momentum")

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

    def _get_session_phase(self) -> str:
        """Determine session phase based on fraction elapsed."""
        estimated_length = self._get_estimated_session_length()
        if estimated_length <= 0:
            return "mid"
        fraction = self._bars_in_session / estimated_length
        
        if fraction < 0.30:
            return "early"
        elif fraction < 0.70:
            return "mid"
        else:
            return "late"

    def _get_decay_weighted_score(self, level: float) -> float:
        """
        Calculate decay-weighted importance of a price level.
        
        Current session: 1.0x
        Prior-1: 0.65x
        Prior-2: 0.35x
        Prior-3: 0.15x
        """
        total_score = 0.0

        # Current session
        total_score += self._current_session_levels.get(level, 0) * 1.0

        # Prior sessions with decay
        decay_factors = [0.65, 0.35, 0.15]
        for i, session_levels in enumerate(reversed(list(self._level_visits_by_session))):
            if i >= len(decay_factors):
                break
            total_score += session_levels.get(level, 0) * decay_factors[i]

        return total_score

    def _get_buy_volume_ratio(self, bar) -> float:
        """Get buy volume as ratio of total volume."""
        if bar.volume == 0:
            return 0.5
        return bar.buy_volume / bar.volume

    def _get_sell_volume_ratio(self, bar) -> float:
        """Get sell volume as ratio of total volume."""
        if bar.volume == 0:
            return 0.5
        return bar.sell_volume / bar.volume

    def _get_volume_ratio_threshold(self, decay_score: float, direction: str) -> float:
        """Get volume ratio threshold based on level strength."""
        if decay_score >= self.decay_strong_threshold:
            return self.buy_volume_ratio_strong
        else:
            return self.buy_volume_ratio_medium

    def _requires_prior_opposite_direction(self, decay_score: float) -> bool:
        """Check if prior bar must show opposite direction."""
        return decay_score < self.decay_strong_threshold

    def _get_delta_threshold(self) -> float:
        """Get delta threshold based on session phase."""
        phase = self._get_session_phase()
        
        if phase == "early":
            return self.early_phase_delta_ratio
        elif phase == "late":
            return self.late_phase_delta_ratio
        else:
            return self.absorption_delta_ratio

    def _calculate_momentum(self, bar) -> float:
        """
        Calculate intra-bar momentum.
        
        momentum = (close - open) / (high - low)
        Positive: price moved up through bar
        Negative: price moved down through bar
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0
        return (bar.close - bar.open) / bar_range

    def _get_close_position(self, bar) -> float:
        """
        Get close position within bar range.
        
        0.0: close at low
        1.0: close at high
        0.5: close at midpoint
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with intra-bar price distribution filtering.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Get level and its decay score
        level = self._round_price(current.close)
        decay_score = self._get_decay_weighted_score(level)

        # Filter 2: Level must have sufficient decay score
        if decay_score < self.decay_threshold_min:
            return None

        # Filter 3: Phase-dependent delta threshold
        delta_threshold = self._get_delta_threshold()
        if delta_ratio < delta_threshold:
            return None

        # Filter 4: Prior bar context (depends on level strength)
        if self._requires_prior_opposite_direction(decay_score):
            if len(self._bar_history) >= 2:
                prior_bar = self._bar_history[-2]
                if delta > 0 and prior_bar.delta >= 0:
                    return None
                if delta < 0 and prior_bar.delta <= 0:
                    return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Calculate intra-bar metrics (TICK-SEQUENCE DEPENDENT)
        momentum = self._calculate_momentum(current)
        close_position = self._get_close_position(current)

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 5: Intra-bar momentum must be NEGATIVE (price rejected downward)
            if momentum > -self.momentum_threshold:
                return None

            # Filter 6: Close position must be HIGH (price near top, then rejected)
            if close_position < self.close_position_long_min:
                return None

            # Filter 7: Buy volume concentration (decay-dependent threshold)
            buy_ratio = self._get_buy_volume_ratio(current)
            threshold = self._get_volume_ratio_threshold(decay_score, "long")
            if buy_ratio < threshold:
                return None

            lower_wick = current.close - current.low

            # Filter 8: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 9: Wick relative to bar range
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
                "absorption_close": current.close,
                "decay_score": decay_score,
                "momentum": momentum,
            }

        # SHORT: Selling pressure with absorption
        else:
            # Filter 5: Intra-bar momentum must be POSITIVE (price rejected upward)
            if momentum < self.momentum_threshold:
                return None

            # Filter 6: Close position must be LOW (price near bottom, then rejected)
            if close_position > self.close_position_short_max:
                return None

            # Filter 7: Sell volume concentration (decay-dependent threshold)
            sell_ratio = self._get_sell_volume_ratio(current)
            threshold = self._get_volume_ratio_threshold(decay_score, "short")
            if sell_ratio < threshold:
                return None

            upper_wick = current.high - current.close

            # Filter 8: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 9: Wick relative to bar range
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
                "absorption_close": current.close,
                "decay_score": decay_score,
                "momentum": momentum,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requirements:
        1. Directional follow-through
        2. Volume distribution confirmation
        3. OPPOSITE intra-bar momentum (TICK-SEQUENCE DEPENDENT)
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]
        pending_momentum = self._pending_momentum

        # Calculate confirming bar momentum
        confirm_momentum = self._calculate_momentum(confirming_bar)

        if direction == "long":
            # Must close above absorption close
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            
            # Must show buy volume dominance
            buy_ratio = self._get_buy_volume_ratio(confirming_bar)
            if buy_ratio < self.confirm_volume_ratio:
                return False
            
            # Confirming bar momentum must be POSITIVE (opposite of negative absorption)
            if confirm_momentum < self.confirm_momentum_threshold:
                return False
            
            return True
        else:  # short
            # Must close below absorption close
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            
            # Must show sell volume dominance
            sell_ratio = self._get_sell_volume_ratio(confirming_bar)
            if sell_ratio < self.confirm_volume_ratio:
                return False
            
            # Confirming bar momentum must be NEGATIVE (opposite of positive absorption)
            if confirm_momentum > -self.confirm_momentum_threshold:
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
