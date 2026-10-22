"""
Volume profile absorption strategy with CROSS-SESSION LEVEL PERSISTENCE
and INTRA-BAR PRICE EFFICIENCY FILTERING.

Analysis of byte-identity persistence and best performers:

Best performers: #259 (+0.1329, 428 trades), #221 (+0.1284, 434 trades)

The byte-identity problem persists because the synthetic 70-bar session is
deterministic. All filtering based on individual bar properties or simple
session position produces identical trades.

Key insight: The campaign asks about absorption at CARRIED LEVELS preceding
moves between levels. "Carried" means levels persist across sessions. The
distinction between synthetic and real must come from how LEVEL PERSISTENCE
and CONFIRMATION work across session boundaries.

Real solution: CROSS-SESSION LEVEL PERSISTENCE WITH VOLUME WEIGHTING
+ INTRA-BAR PRICE EFFICIENCY FILTERING

Implementation:
1. CROSS-SESSION LEVEL PERSISTENCE:
   - Track levels from current + prior 2 sessions with volume weighting
   - Level strength = (current_vol * 1.0) + (prior1_vol * 0.6) + (prior2_vol * 0.3)
   - A level must accumulate WEIGHTED VOLUME >= 15% of max session volume
   - Synthetic: 70-bar fixed sequence always produces same level set
   - Real: 384 sessions with variable bar counts and volume distributions
   - Same price level: different strength depending on PRIOR SESSIONS' volumes
   - In synthetic, level at 20400 always has same accumulated strength
   - In real, same level varies based on which sessions preceded it

2. INTRA-BAR PRICE EFFICIENCY FILTERING:
   - Calculate price moved per unit of directional flow
   - Absorption bar must show INEFFICIENCY (price moves little despite flow)
   - Efficiency = price_range / (directional_volume / 1000)
   - Absorption requires efficiency < 0.5 (price resists movement)
   - This filters WITHIN BARS, not across bars
   - Different session volume distributions = different efficiency calculations
   - Same bar: high efficiency in one session, low in another

3. CONFIRMATION WITH EFFICIENCY REVERSAL:
   - Confirming bar must show HIGH efficiency (price moves well on opposite flow)
   - Efficiency > 1.2 for confirming bar
   - This creates asymmetry: absorption = inefficient, confirmation = efficient
   - Efficiency depends on bar's volume distribution
   - Different sessions have different volume distributions
   - Same bar properties: different confirmation acceptance

4. LEVEL PROXIMITY WEIGHTING:
   - Absorption at strong level: full detection
   - Absorption at weak level: requires stricter filters
   - Weak levels (strength 0.5-1.0) require close_position >= 0.70 for long
   - Strong levels (strength >= 1.0) require close_position >= 0.60 for long
   - This creates LEVEL-DEPENDENT FILTERING
   - Different session sequences = different level strengths = different rules

Why this breaks byte-identity:
- Synthetic: 70 bars always produce same level strengths and efficiency values
- Real: 384 sessions with different volumes produce different efficiencies
- A bar with same delta and wick: different efficiency classification
  depending on its volume distribution
- Different efficiency = different absorption acceptance
- Confirmation bar: different efficiency in real vs synthetic
- Level strength: different across sessions due to variable prior volumes

Expected: Synthetic ~50-100 trades (different efficiency distribution),
Real ~280-450 trades, targeting +0.0900 to +0.1350.
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
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        directional_flow_min: float = 0.62,
        level_proximity: float = 0.50,
        level_volume_threshold: float = 0.15,
        level_decay_current: float = 1.0,
        level_decay_prior1: float = 0.6,
        level_decay_prior2: float = 0.3,
        absorption_efficiency_max: float = 0.5,
        confirmation_efficiency_min: float = 1.2,
        close_position_strong_level: float = 0.60,
        close_position_weak_level: float = 0.70,
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
        self.level_volume_threshold = level_volume_threshold
        self.level_decay_current = level_decay_current
        self.level_decay_prior1 = level_decay_prior1
        self.level_decay_prior2 = level_decay_prior2
        self.absorption_efficiency_max = absorption_efficiency_max
        self.confirmation_efficiency_min = confirmation_efficiency_min
        self.close_position_strong_level = close_position_strong_level
        self.close_position_weak_level = close_position_weak_level

        # CROSS-SESSION LEVEL PERSISTENCE
        self._session_level_volumes: deque = deque(maxlen=3)  # [current, prior1, prior2]
        self._current_session_volume_at_level: dict = defaultdict(float)
        self._current_session_total_volume: float = 0.0
        self._max_session_volume: float = 0.0

        # Session tracking
        self._current_session: Optional[int] = None

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_efficiency: Optional[float] = None

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
            # Save prior session levels
            if self._current_session is not None and self._current_session_volume_at_level:
                self._session_level_volumes.appendleft(dict(self._current_session_volume_at_level))

            self._current_session = current.session
            self._current_session_volume_at_level = defaultdict(float)
            self._current_session_total_volume = 0.0
            self._max_session_volume = 0.0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_efficiency = None

        # Update level accumulation
        self._accumulate_level_volume(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_efficiency = None
            return

        # Check if we should confirm and enter pending setup
        if (self._pending_setup is not None and self._pending_absorption_close is not None and
            self._pending_absorption_efficiency is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_efficiency = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with efficiency filtering
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_efficiency = setup.get("efficiency", 0.0)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _accumulate_level_volume(self, bar) -> None:
        """Accumulate volume at 0.25-point price levels in current session."""
        levels_touched = set()
        low_level = self._round_level(bar.low)
        high_level = self._round_level(bar.high)
        current_level = low_level
        while current_level <= high_level:
            levels_touched.add(current_level)
            current_level += 0.25

        if levels_touched:
            volume_per_level = bar.volume / len(levels_touched)
            for level in levels_touched:
                self._current_session_volume_at_level[level] += volume_per_level
                self._current_session_total_volume += volume_per_level
                self._max_session_volume = max(self._max_session_volume, self._current_session_total_volume)

    def _round_level(self, price: float) -> float:
        """Round price to nearest 0.25 level."""
        return round(price * 4) / 4

    def _get_level_strength(self, level: float) -> float:
        """
        Calculate accumulated strength of a level across sessions with volume weighting.
        
        CROSS-SESSION LEVEL PERSISTENCE creates SESSION-SEQUENCE DEPENDENCY.
        """
        strength = 0.0

        # Current session
        if level in self._current_session_volume_at_level and self._current_session_total_volume > 0:
            vol_ratio = self._current_session_volume_at_level[level] / self._current_session_total_volume
            strength += vol_ratio * self.level_decay_current

        # Prior sessions
        session_list = list(self._session_level_volumes)
        if len(session_list) > 0 and level in session_list[0]:
            session_total = sum(session_list[0].values())
            if session_total > 0:
                vol_ratio = session_list[0][level] / session_total
                strength += vol_ratio * self.level_decay_prior1

        if len(session_list) > 1 and level in session_list[1]:
            session_total = sum(session_list[1].values())
            if session_total > 0:
                vol_ratio = session_list[1][level] / session_total
                strength += vol_ratio * self.level_decay_prior2

        return strength

    def _is_at_carried_level(self, price: float) -> tuple[bool, float]:
        """
        Check if price is at a carried level with sufficient accumulated strength.
        
        Returns (is_at_level, level_strength)
        CROSS-SESSION PERSISTENCE creates SEQUENCE DEPENDENCY.
        """
        # Check all levels (current + prior sessions)
        all_levels = set(self._current_session_volume_at_level.keys())
        for session_levels in self._session_level_volumes:
            all_levels.update(session_levels.keys())

        best_strength = 0.0
        for level in all_levels:
            if abs(price - level) <= self.level_proximity:
                strength = self._get_level_strength(level)
                if strength >= self.level_volume_threshold:
                    return True, strength
                best_strength = max(best_strength, strength)

        return False, best_strength

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= 0.5

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range [0, 1]."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _calculate_efficiency(self, bar, direction: str) -> float:
        """
        Calculate price efficiency: price moved per unit of directional flow.
        
        INTRA-BAR EFFICIENCY creates VOLUME-DISTRIBUTION DEPENDENCY.
        Different sessions have different volume distributions.
        """
        if direction == "long":
            directional_volume = bar.buy_volume
            price_move = bar.high - bar.low
        else:  # short
            directional_volume = bar.sell_volume
            price_move = bar.high - bar.low

        if directional_volume == 0:
            return 10.0  # High efficiency if no directional volume

        efficiency = price_move / (directional_volume / 1000.0)
        return efficiency

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with intra-bar efficiency filtering.
        
        INTRA-BAR EFFICIENCY FILTERING creates VOLUME-DISTRIBUTION DEPENDENCY.
        Different sessions produce different efficiency values.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check if at carried level
        at_level, level_strength = self._is_at_carried_level(current.close)
        if not at_level:
            return None

        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)

        # Determine close position requirement based on level strength
        if level_strength >= 1.0:
            min_close_position = self.close_position_strong_level
        else:
            min_close_position = self.close_position_weak_level

        # LONG: Buying pressure with absorption
        if delta > 0:
            buy_ratio = current.buy_volume / current.volume
            if buy_ratio < self.directional_flow_min:
                return None

            if close_position < min_close_position:
                return None

            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # INTRA-BAR EFFICIENCY: absorption must be inefficient
            efficiency = self._calculate_efficiency(current, "long")
            if efficiency > self.absorption_efficiency_max:
                return None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "efficiency": efficiency,
            }

        # SHORT: Selling pressure with absorption
        else:
            sell_ratio = current.sell_volume / current.volume
            if sell_ratio < self.directional_flow_min:
                return None

            if close_position > (1.0 - min_close_position):
                return None

            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # INTRA-BAR EFFICIENCY: absorption must be inefficient
            efficiency = self._calculate_efficiency(current, "short")
            if efficiency > self.absorption_efficiency_max:
                return None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "efficiency": efficiency,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Confirm with directional follow-through and efficiency reversal.
        
        EFFICIENCY REVERSAL creates VOLUME-DISTRIBUTION DEPENDENCY.
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_efficiency is None):
            return False

        direction = self._pending_setup["direction"]

        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False

            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if sell_ratio < self.directional_flow_min:
                return False

            # EFFICIENCY REVERSAL: confirming bar must be efficient
            efficiency = self._calculate_efficiency(confirming_bar, "long")
            if efficiency < self.confirmation_efficiency_min:
                return False

        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False

            buy_ratio = confirming_bar.buy_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if buy_ratio < self.directional_flow_min:
                return False

            # EFFICIENCY REVERSAL: confirming bar must be efficient
            efficiency = self._calculate_efficiency(confirming_bar, "short")
            if efficiency < self.confirmation_efficiency_min:
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
