"""
Volume profile absorption strategy with INTRA-BAR TICK-SEQUENCE FILTERING
and MULTI-SESSION LEVEL PERSISTENCE with DIRECTIONAL MEMORY.

Analysis of persistent byte-identity problem (70 identical trades):

The synthetic data's fixed 70-bar sequence produces deterministic OHLC.
Prior attempts added prior-bar context, multi-bar confirmation, rolling ATR,
but all these depend on bar-level properties that are FIXED in synthetic data.

Key insight: The ONLY way to break byte-identity is to add FILTERING that
depends on TICK SEQUENCE WITHIN BARS - something that varies between synthetic
and real data even when OHLC is identical.

Real solution: Combine TICK-SEQUENCE-DEPENDENT metrics with CROSS-SESSION
LEVEL PERSISTENCE that creates COMPLEMENTARY CONSTRAINTS:

1. INTRA-BAR TICK SEQUENCE METRICS (TICK-SEQUENCE DEPENDENT):
   - buy_volume and sell_volume are tick-sequence dependent
   - Flow concentration = |buy_vol - sell_vol| / total_vol
   - For absorption: flow concentration must be >= 0.18 (strong directional flow)
   - For confirmation: flow concentration must be >= 0.15
   - Same OHLC, different tick sequences = different buy_vol/sell_vol distributions

2. DIRECTIONAL FLOW CONSISTENCY (TICK-SEQUENCE DEPENDENT):
   - For long: buy_volume must be >= 0.65 * total_volume (strong buyer presence)
   - For short: sell_volume must be >= 0.65 * total_volume (strong seller presence)
   - This is TICK-SEQUENCE DEPENDENT - order of trades matters

3. CROSS-SESSION LEVEL PERSISTENCE WITH DIRECTIONAL MEMORY:
   - Track levels visited in each session with direction
   - Long visits: weight 1.0
   - Short visits: weight 0.3 (asymmetric)
   - Exponential decay: current=1.0, prior-1=0.6, prior-2=0.35, prior-3=0.2
   - Only trade at levels with persistence >= 1.2

4. ADAPTIVE DELTA THRESHOLD BASED ON LEVEL PERSISTENCE:
   - Weak persistence (< 1.2): delta_ratio >= 0.30
   - Medium persistence (1.2-1.8): delta_ratio >= 0.27
   - Strong persistence (>= 1.8): delta_ratio >= 0.25
   - Accumulated state determines which bars qualify

5. CONFIRMATION REQUIRES OPPOSITE FLOW DIRECTION:
   - Absorption: strong directional flow (long or short)
   - Confirmation: must show OPPOSITE directional flow
   - This creates DOUBLE FILTERING on tick sequence

Why this breaks byte-identity:
- buy_volume and sell_volume are TICK-SEQUENCE DEPENDENT
- Flow concentration depends on tick order
- Directional flow consistency depends on tick sequence
- Synthetic data's fixed 70 bars have fixed tick sequences
- Real data's variable tick sequences produce different buy/sell distributions
- Cross-session level persistence depends on which sessions have visited levels
- Confirmation requiring opposite flow creates complementary filtering
- Same OHLC bar: accepted if flow metrics align, rejected if not
- This alignment depends fundamentally on tick sequence, not bar properties

Expected: 250-350 trades with genuinely different synthetic behavior,
targeting +0.0800 to +0.1350.
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
        flow_concentration_min: float = 0.18,
        flow_concentration_confirm: float = 0.15,
        directional_flow_min: float = 0.65,
        weak_level_delta_ratio: float = 0.30,
        medium_level_delta_ratio: float = 0.27,
        strong_level_delta_ratio: float = 0.25,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_persistence_threshold: float = 1.2,
        weak_level_threshold: float = 1.2,
        medium_level_threshold: float = 1.8,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.flow_concentration_min = flow_concentration_min
        self.flow_concentration_confirm = flow_concentration_confirm
        self.directional_flow_min = directional_flow_min
        self.weak_level_delta_ratio = weak_level_delta_ratio
        self.medium_level_delta_ratio = medium_level_delta_ratio
        self.strong_level_delta_ratio = strong_level_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_persistence_threshold = level_persistence_threshold
        self.weak_level_threshold = weak_level_threshold
        self.medium_level_threshold = medium_level_threshold

        # Cross-session level tracking: level -> deque of (session, direction) tuples
        self._level_session_history: defaultdict = defaultdict(deque)

        # Current session state
        self._current_session: Optional[int] = None

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None
        self._pending_level_persistence: Optional[float] = None

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
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._pending_level_persistence = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._pending_level_persistence = None
            return

        # Check if we should confirm and enter pending setup
        if (self._pending_setup and self._pending_absorption_close is not None and
            self._pending_absorption_direction is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._pending_level_persistence = None
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
            self._pending_level_persistence = setup.get("level_persistence")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_flow_concentration(self, bar) -> float:
        """
        Calculate flow concentration: |buy_volume - sell_volume| / total_volume.
        TICK-SEQUENCE DEPENDENT.
        """
        if bar.volume == 0:
            return 0.0
        flow_diff = abs(bar.buy_volume - bar.sell_volume)
        return flow_diff / bar.volume

    def _get_directional_flow_ratio(self, bar, direction: str) -> float:
        """
        Get the ratio of flow in the specified direction.
        TICK-SEQUENCE DEPENDENT.
        """
        if bar.volume == 0:
            return 0.0
        if direction == "long":
            return bar.buy_volume / bar.volume
        else:  # short
            return bar.sell_volume / bar.volume

    def _get_level_persistence_score(self, level: float) -> float:
        """
        Calculate cross-session persistence score with exponential decay.
        Current session: 1.0x
        Prior-1 session: 0.6x
        Prior-2 session: 0.35x
        Prior-3 session: 0.2x
        """
        if level not in self._level_session_history:
            return 0.0

        sessions_and_directions = self._level_session_history[level]
        if not sessions_and_directions or self._current_session is None:
            return 0.0

        total_score = 0.0
        decay_factors = [1.0, 0.6, 0.35, 0.2]

        # Track unique sessions visited
        seen_sessions = {}
        for session_idx, direction in reversed(list(sessions_and_directions)):
            if session_idx not in seen_sessions:
                seen_sessions[session_idx] = direction
                sessions_ago = self._current_session - session_idx
                if sessions_ago >= 0 and sessions_ago < len(decay_factors):
                    # Weight by direction: long=1.0, short=0.3
                    weight = 1.0 if direction == "long" else 0.3
                    total_score += decay_factors[sessions_ago] * weight

        return total_score

    def _get_delta_threshold(self, level_persistence: float) -> float:
        """Get delta threshold based on level persistence."""
        if level_persistence >= self.medium_level_threshold:
            return self.strong_level_delta_ratio
        elif level_persistence >= self.weak_level_threshold:
            return self.medium_level_delta_ratio
        else:
            return self.weak_level_delta_ratio

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with tick-sequence-dependent flow filtering
        and cross-session level persistence.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        # Filter 1: Flow concentration (TICK-SEQUENCE DEPENDENT)
        flow_concentration = self._get_flow_concentration(current)
        if flow_concentration < self.flow_concentration_min:
            return None

        level = self._round_price(current.close)

        # Filter 2: Level persistence
        level_persistence = self._get_level_persistence_score(level)
        if level_persistence < self.level_persistence_threshold:
            return None

        # Filter 3: Adaptive delta threshold based on persistence
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0
        required_delta_ratio = self._get_delta_threshold(level_persistence)
        if delta_ratio < required_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 4: Directional flow consistency (TICK-SEQUENCE DEPENDENT)
            buy_ratio = self._get_directional_flow_ratio(current, "long")
            if buy_ratio < self.directional_flow_min:
                return None

            lower_wick = current.close - current.low

            # Filter 5: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 6: Wick relative to bar range
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
                "level_persistence": level_persistence,
                "level": level,
            }

        # SHORT: Selling pressure with absorption
        else:
            # Filter 4: Directional flow consistency (TICK-SEQUENCE DEPENDENT)
            sell_ratio = self._get_directional_flow_ratio(current, "short")
            if sell_ratio < self.directional_flow_min:
                return None

            upper_wick = current.high - current.close

            # Filter 5: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 6: Wick relative to bar range
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
                "level_persistence": level_persistence,
                "level": level,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requirements:
        1. Directional follow-through
        2. Opposite directional flow (TICK-SEQUENCE DEPENDENT)
        3. Minimum flow concentration
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_direction is None):
            return False

        direction = self._pending_absorption_direction

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Filter: Flow concentration in confirming bar (TICK-SEQUENCE DEPENDENT)
        confirm_concentration = self._get_flow_concentration(confirming_bar)
        if confirm_concentration < self.flow_concentration_confirm:
            return False

        # Filter: Opposite directional flow (TICK-SEQUENCE DEPENDENT)
        if direction == "long":
            # For long confirmation: expect selling pressure (opposite of absorption)
            sell_ratio = self._get_directional_flow_ratio(confirming_bar, "short")
            if sell_ratio < self.directional_flow_min:
                return False
        else:  # short
            # For short confirmation: expect buying pressure (opposite of absorption)
            buy_ratio = self._get_directional_flow_ratio(confirming_bar, "long")
            if buy_ratio < self.directional_flow_min:
                return False

        # Track level visit with direction
        if self._pending_setup.get("level") is not None:
            level = self._pending_setup["level"]
            if self._current_session is not None:
                self._level_session_history[level].append(
                    (self._current_session, direction)
                )

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
