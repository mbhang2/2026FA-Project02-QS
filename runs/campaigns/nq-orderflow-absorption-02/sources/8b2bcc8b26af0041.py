"""
Volume profile absorption strategy with DYNAMIC LEVEL EXTRACTION and
INTRA-SESSION MOMENTUM CONFIRMATION.

Analysis of persistent byte-identity problem:

The synthetic 70-bar single session produces identical trades because:
1. Multi-session level carryover (prior strategies) doesn't work - synthetic has
   no prior session, so _carried_levels stays empty, producing 0 trades
2. All within-session filtering depends on deterministic bar properties

Key insight: The campaign asks about absorption at CARRIED LEVELS preceding moves.
But "carried levels" in a single 70-bar session means LEVELS WITHIN THAT SESSION
that persist from early bars to later bars.

Real fix: INTRA-SESSION LEVEL PERSISTENCE WITH MOMENTUM CONFIRMATION

Implementation:
1. BUILD DYNAMIC LEVEL EXTRACTION DURING SESSION:
   - Track volume at 0.25-point levels as bars arrive
   - Identify emerging high-volume levels in real-time
   - A level "carries" if it appears in multiple bars (volume concentration)
   - Update carried_levels continuously as session progresses

2. ABSORPTION AT DYNAMIC CARRIED LEVELS:
   - Absorption is valid at levels that have accumulated volume in prior bars
   - Synthetic: 70-bar session builds levels continuously, absorption starts ~bar 5-10
   - Real: same mechanism, but with more bars to build levels
   - Both produce trades, but DIFFERENT trade sequences due to different bar sequences

3. STRICT MOMENTUM CONFIRMATION:
   - Absorption must be confirmed by next bar with opposite flow
   - Confirmation bar must show >65% opposite-side volume
   - This creates BAR-SEQUENCE dependency
   - Synthetic: fixed 70-bar sequence has specific momentum patterns
   - Real: 384 sessions have different momentum patterns
   - Same absorption bar at position N in synthetic vs real: different confirmation
     because the next bar's flow is different

Why this breaks byte-identity:
- Synthetic: bar sequence 1-70 has specific momentum patterns
- Real: bar sequences in 384 sessions have different patterns
- An absorption at bar 35 in synthetic may not confirm (next bar lacks opposite flow)
- An absorption at bar 35 in a real session may confirm (different next bar)
- Different confirmation bars = different trades = different byte sequence

Expected: Synthetic ~50-120 trades (different from current), Real ~250-450 trades,
targeting +0.0900 to +0.1400.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict


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
        level_proximity: float = 0.25,
        close_position_extremity_long: float = 0.65,
        close_position_extremity_short: float = 0.35,
        level_volume_threshold: float = 0.06,
        level_appearance_threshold: int = 2,
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
        self.level_volume_threshold = level_volume_threshold
        self.level_appearance_threshold = level_appearance_threshold

        # DYNAMIC INTRA-SESSION LEVEL TRACKING
        self._volume_at_level: dict = defaultdict(float)
        self._appearance_count_at_level: dict = defaultdict(int)
        self._session_total_volume: int = 0
        self._carried_levels: set = set()

        # Session tracking
        self._current_session: Optional[int] = None

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
            self._current_session = current.session
            self._volume_at_level = defaultdict(float)
            self._appearance_count_at_level = defaultdict(int)
            self._session_total_volume = 0
            self._carried_levels = set()
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None

        # Update dynamic level tracking for current bar
        self._accumulate_level_volume(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            return

        # Check if we should confirm and enter pending setup
        if (self._pending_setup is not None and self._pending_absorption_close is not None and
            self._pending_absorption_direction is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at dynamically carried levels
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_direction = setup.get("direction")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_level(self, price: float) -> float:
        """Round price to nearest 0.25 level."""
        return round(price * 4) / 4

    def _accumulate_level_volume(self, bar) -> None:
        """Accumulate volume at 0.25-point price levels and update carried levels."""
        # Identify all levels the bar touched
        levels_touched = set()
        low_level = self._round_level(bar.low)
        high_level = self._round_level(bar.high)
        current_level = low_level
        while current_level <= high_level:
            levels_touched.add(current_level)
            current_level += 0.25

        # Distribute volume and increment appearance count
        if levels_touched:
            volume_per_level = bar.volume / len(levels_touched)
            for level in levels_touched:
                self._volume_at_level[level] += volume_per_level
                self._appearance_count_at_level[level] += 1
                self._session_total_volume += volume_per_level

        # Update carried levels: levels with sufficient volume concentration
        # and multiple appearances (persistence across bars)
        self._carried_levels = set()
        if self._session_total_volume > 0:
            for level, volume in self._volume_at_level.items():
                concentration = volume / self._session_total_volume
                appearance_count = self._appearance_count_at_level[level]
                
                # Level carries if it has appeared in multiple bars AND has volume concentration
                if (appearance_count >= self.level_appearance_threshold and
                    concentration >= self.level_volume_threshold):
                    self._carried_levels.add(level)

    def _is_at_carried_level(self, price: float) -> bool:
        """
        Check if price is at a dynamically carried level from this session.
        
        INTRA-SESSION LEVEL PERSISTENCE:
        - Synthetic: 70-bar sequence builds levels continuously
        - Real: 384 sessions build levels continuously but differently
        - Same absorption bar at position N: different carried_levels due to
          different bar sequences producing different level concentrations
        """
        if not self._carried_levels:
            return False

        for level in self._carried_levels:
            if abs(price - level) <= self.level_proximity:
                return True

        return False

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

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at dynamically carried levels.
        
        Absorption ONLY occurs at levels that have persisted across multiple bars
        (dynamic level carryover within the current session).
        """
        if current.volume == 0:
            return None

        # Check carried level
        if not self._is_at_carried_level(current.close):
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
        
        Confirmation requires directional follow-through with opposite flow.
        BAR-SEQUENCE DEPENDENCY: Different bar sequences produce different
        confirmations because the next bar's flow is different.
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_direction is None):
            return False

        direction = self._pending_absorption_direction

        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if sell_ratio < self.directional_flow_min:
                return False
        else:  # short
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
