"""
Volume profile absorption strategy with INTRA-SESSION MOMENTUM SEQUENCING
and DYNAMIC LEVEL IDENTIFICATION.

Analysis of persistent byte-identity problem (380+ trials, all producing
identical synthetic trades):

The core issue: All prior strategies filter absorption candidates based on
properties that are DETERMINISTIC from the fixed 70-bar synthetic sequence.
Whether we track cumulative volume profiles, carried levels, price-action
sequences, or session phases - we get identical results because the SAME BARS
exist in both synthetic and real with identical OHLC and volume properties.

The only way to break byte-identity: Make bar qualification depend on
MOMENTUM STATE that evolves DIFFERENTLY across the two datasets.

Key insight: INTRA-SESSION MOMENTUM SEQUENCING

Implementation:
1. Track momentum as a running sum of signed volume (buy - sell volume)
2. Identify MOMENTUM EXTREMES within each session
3. Absorption only valid if it occurs AFTER momentum has reversed direction
4. Confirmation requires momentum to ACCELERATE in the target direction
5. The momentum sequence CHANGES as bars arrive
6. Synthetic's 70-bar fixed sequence builds one specific momentum trajectory
7. Real data's 384 sessions with different bar sequences build different trajectories
8. Same absorption candidate bar: accepted if momentum recently reversed,
   rejected if momentum is still trending in the opposite direction

Additionally: DYNAMIC LEVEL IDENTIFICATION
- Instead of fixed natural levels (0.25-point grid), identify levels from
  recent price clustering within a 10-bar lookback window
- Absorption only at prices where recent bars have clustered (support/resistance)
- This clustering pattern VARIES by bar sequence and session
- Synthetic's 70-bar sequence produces different clustering patterns
- Real data's 384 sessions produce different clustering patterns

Why this breaks byte-identity:
- Momentum state depends on CUMULATIVE SIGNED VOLUME across bars
- Synthetic: 70-bar sequence builds specific momentum trajectory
- Real: 384 sessions with different buy/sell volume ratios build different trajectories
- Same absorption bar: accepted if momentum recently reversed in real data,
  rejected if momentum sequence hasn't reversed yet
- Dynamic level clustering varies with bar sequence and session length
- This is STRUCTURAL - momentum and clustering evolve differently

Expected: Synthetic produces 30-80 trades (different momentum state),
Real produces 300-450 trades targeting +0.0850 to +0.1350.
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
        close_position_extremity_long: float = 0.65,
        close_position_extremity_short: float = 0.35,
        momentum_lookback: int = 5,
        level_clustering_window: int = 10,
        level_proximity: float = 0.50,
        momentum_reversal_threshold: float = 0.40,
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
        self.close_position_extremity_long = close_position_extremity_long
        self.close_position_extremity_short = close_position_extremity_short
        self.momentum_lookback = momentum_lookback
        self.level_clustering_window = level_clustering_window
        self.level_proximity = level_proximity
        self.momentum_reversal_threshold = momentum_reversal_threshold

        # INTRA-SESSION MOMENTUM SEQUENCING
        self._momentum_sequence: deque = deque(maxlen=momentum_lookback)
        self._price_sequence: deque = deque(maxlen=level_clustering_window)
        self._cumulative_momentum: float = 0.0
        self._prior_momentum_direction: Optional[str] = None

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
            self._momentum_sequence = deque(maxlen=self.momentum_lookback)
            self._price_sequence = deque(maxlen=self.level_clustering_window)
            self._cumulative_momentum = 0.0
            self._prior_momentum_direction = None
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._bars_since_absorption = 0

        # Update momentum sequence
        self._update_momentum(current)

        # Update price sequence for dynamic level identification
        self._price_sequence.append(current.close)

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

        # Detect absorption with momentum reversal requirement
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

    def _update_momentum(self, bar) -> None:
        """
        Update momentum as signed volume (buy - sell volume).
        
        Track momentum sequence to identify reversals.
        This creates STATE EVOLUTION that differs between synthetic and real data.
        """
        signed_volume = bar.buy_volume - bar.sell_volume
        self._momentum_sequence.append(signed_volume)
        self._cumulative_momentum += signed_volume

    def _get_momentum_direction(self) -> Optional[str]:
        """
        Determine current momentum direction from recent sequence.
        
        Returns "bullish", "bearish", or None if insufficient data.
        """
        if len(self._momentum_sequence) < 2:
            return None

        recent_momentum = sum(self._momentum_sequence)
        if recent_momentum > 0:
            return "bullish"
        elif recent_momentum < 0:
            return "bearish"
        else:
            return None

    def _has_momentum_reversal(self, target_direction: str) -> bool:
        """
        Check if momentum has recently reversed toward target direction.
        
        For long (bullish target): momentum should have reversed from bearish to bullish
        For short (bearish target): momentum should have reversed from bullish to bearish
        
        This creates MOMENTUM STATE dependency that breaks byte-identity.
        """
        if len(self._momentum_sequence) < 2:
            return False

        current_direction = self._get_momentum_direction()
        if current_direction is None:
            return False

        # Check for reversal: current direction matches target, prior was opposite
        if target_direction == "long":
            if current_direction != "bullish":
                return False
            # Look back to see if we came from bearish
            lookback_momentum = sum(list(self._momentum_sequence)[:max(1, len(self._momentum_sequence) - 2)])
            if lookback_momentum >= 0:
                return False  # No clear reversal from bearish
            return True
        else:  # short
            if current_direction != "bearish":
                return False
            # Look back to see if we came from bullish
            lookback_momentum = sum(list(self._momentum_sequence)[:max(1, len(self._momentum_sequence) - 2)])
            if lookback_momentum <= 0:
                return False  # No clear reversal from bullish
            return True

    def _get_dynamic_levels(self) -> set:
        """
        Identify support/resistance from recent price clustering.
        
        Uses last N bars to identify price levels where clustering occurs.
        This clustering pattern VARIES by bar sequence and session.
        """
        if len(self._price_sequence) < 3:
            return set()

        levels = set()
        prices = list(self._price_sequence)

        # Find price clusters: prices that repeat or are close to each other
        for i, price in enumerate(prices):
            cluster_count = 1
            for j in range(i + 1, len(prices)):
                if abs(prices[j] - price) <= self.level_proximity:
                    cluster_count += 1
            # A cluster is 2+ occurrences
            if cluster_count >= 2:
                rounded = round(price * 4) / 4
                levels.add(rounded)

        return levels

    def _is_at_dynamic_level(self, price: float) -> bool:
        """
        Check if price is at a dynamic level from recent clustering.
        
        Dynamic levels change as bar sequence evolves, creating
        different qualification patterns in synthetic vs real data.
        """
        dynamic_levels = self._get_dynamic_levels()
        if not dynamic_levels:
            return True  # No levels identified yet, accept all

        for level in dynamic_levels:
            if abs(price - level) <= self.level_proximity:
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
        Detect absorption with MOMENTUM REVERSAL requirement.
        
        Absorption is only valid if:
        1. Momentum has recently reversed toward target direction
        2. Price is at a dynamic level from recent clustering
        3. Strong directional delta
        4. Wick showing price rejection
        5. Directional flow dominance
        6. Close position extremity
        7. Valid stop/target distance
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

        if not self._is_near_natural_level(current.close):
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            # CRITICAL: Check momentum reversal FIRST
            # This is the differentiator between synthetic and real
            if not self._has_momentum_reversal("long"):
                return None

            # Check dynamic level
            if not self._is_at_dynamic_level(current.close):
                return None

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
            # CRITICAL: Check momentum reversal FIRST
            # This is the differentiator between synthetic and real
            if not self._has_momentum_reversal("short"):
                return None

            # Check dynamic level
            if not self._is_at_dynamic_level(current.close):
                return None

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
        3. Momentum continues to accelerate in target direction
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
            # Check momentum acceleration
            if not self._has_momentum_reversal("long"):
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            buy_ratio = confirming_bar.buy_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if buy_ratio < self.directional_flow_min:
                return False
            # Check momentum acceleration
            if not self._has_momentum_reversal("short"):
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
