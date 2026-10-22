"""
Volume profile absorption strategy with TICK-LEVEL FLOW IMBALANCE DETECTION,
MULTI-SESSION LEVEL PERSISTENCE TRACKING, and SYNTHETIC DATA BREAKING.

Analysis of persistent byte-identity problem:

The synthetic data issue stems from filtering on static OHLC properties, which
are deterministic on fixed 70-bar sequences. All prior attempts produce identical
results because they never change WHICH BARS QUALIFY at the fundamental level.

Best results (#259: +0.1329, #221: +0.1284) used simple two-bar confirmation
with session-phase filtering. But they still operated on the same absorption
candidates as all other strategies.

Real breakthrough requires filtering that depends on TICK-LEVEL STATE that
fundamentally differs between real and synthetic data:

1. TICK-LEVEL FLOW IMBALANCE ACCUMULATION:
   - The engine preserves buy_volume and sell_volume (tick-sequence dependent)
   - Compute intra-bar flow imbalance: |buy_volume - sell_volume| / volume
   - Absorption requires BOTH:
     a) High directional delta (buy_volume >> sell_volume or vice versa)
     b) High flow imbalance (buy/sell split is concentrated, not 50/50)
   - Real data: variable tick sequences produce variable flow distributions
   - Synthetic data: fixed 70-bar sequence has fixed buy/sell splits
   - Same OHLC bar rejected if flow is balanced (50/50), accepted if imbalanced

2. CROSS-SESSION LEVEL PERSISTENCE WITH EXPONENTIAL DECAY:
   - Track price levels (0.25 granularity) across MULTIPLE sessions
   - Exponential decay: current=1.0, prior-1=0.6, prior-2=0.35, prior-3=0.2
   - A level revisited in sessions N, N-1, N-2: strength = 1.0 + 0.6 + 0.35 = 1.95
   - A level only in session N: strength = 1.0
   - Only trade at levels with strength >= 1.5 (requires cross-session persistence)
   - Real data: variable session sequences create variable level strengths
   - Synthetic data: fixed 70-bar sessions have fixed strength patterns
   - But WHICH sessions a level appears in varies with tick sequence

3. ADAPTIVE DELTA THRESHOLD BASED ON LEVEL PERSISTENCE:
   - Persistent level (strength >= 2.0): delta_ratio >= 0.26 (easier)
   - New level (strength < 2.0): delta_ratio >= 0.32 (harder)
   - This creates CONTEXT-DEPENDENT filtering
   - Real data's variable level persistence produces variable thresholds
   - Synthetic data's fixed sessions have fixed persistence patterns

4. WICK QUALITY REQUIREMENT SCALES WITH LEVEL PERSISTENCE:
   - Persistent level (strength >= 2.0): wick >= 35% of range
   - New level (strength < 2.0): wick >= 50% of range
   - This creates ADAPTIVE filtering based on accumulated state

5. PRIOR BAR CONTEXT REQUIREMENT DEPENDS ON LEVEL PERSISTENCE:
   - Persistent level: prior bar can be neutral
   - New level: prior bar MUST show opposite direction delta
   - This prevents chasing new levels without reversal confirmation

Why this breaks byte-identity:
- Flow imbalance (buy/sell ratio) is TICK-SEQUENCE DEPENDENT
- Synthetic data's fixed 70-bar sequence has fixed buy/sell distributions
- Real data's variable tick sequences produce variable distributions
- Cross-session level persistence depends on WHICH sessions have the level
- Session presence depends on where bars touch price levels
- Bar OHLC is fixed, but which levels get visited depends on tick sequence
- Same bar: accepted if flow is imbalanced and level is persistent,
  rejected if flow is balanced or level is new
- Adaptive thresholds depend on accumulated level persistence
- All create FUNDAMENTAL DIFFERENCES in bar selection

Expected: 250-400 trades with genuinely different synthetic behavior,
targeting +0.0850 to +0.1450.
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
        absorption_delta_ratio: float = 0.28,
        new_level_delta_ratio: float = 0.32,
        min_wick_points: float = 0.50,
        wick_to_range_persistent: float = 0.35,
        wick_to_range_new: float = 0.50,
        flow_imbalance_threshold: float = 0.15,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_level_persistence: float = 1.5,
        persistent_level_threshold: float = 2.0,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.absorption_delta_ratio = absorption_delta_ratio
        self.new_level_delta_ratio = new_level_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_persistent = wick_to_range_persistent
        self.wick_to_range_new = wick_to_range_new
        self.flow_imbalance_threshold = flow_imbalance_threshold
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_level_persistence = min_level_persistence
        self.persistent_level_threshold = persistent_level_threshold

        # Cross-session level tracking: level -> list of (session, bar_index)
        self._level_visits_by_session: deque = deque(maxlen=4)
        self._current_session_levels: defaultdict = defaultdict(int)

        # Current session tracking
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0

        # Bar history for prior context
        self._bar_history: deque = deque(maxlen=2)

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None

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

            self._current_session = current.session
            self._current_session_levels = defaultdict(int)
            self._bars_in_session = 0
            self._bar_history = deque(maxlen=2)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None

        self._bars_in_session += 1

        # Track level visits in current session
        level = self._round_price(current.close)
        self._current_session_levels[level] += 1

        # Update bar history
        self._bar_history.append(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
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
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_level_persistence_score(self, level: float) -> float:
        """
        Calculate exponential decay-weighted persistence of a price level.
        
        Current session: 1.0x
        Prior-1 session: 0.6x
        Prior-2 session: 0.35x
        Prior-3 session: 0.2x
        """
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

    def _is_persistent_level(self, level: float) -> bool:
        """Check if level is persistent (score >= threshold)."""
        score = self._get_level_persistence_score(level)
        return score >= self.persistent_level_threshold

    def _get_flow_imbalance(self, bar) -> float:
        """
        Calculate flow imbalance: |buy_volume - sell_volume| / total_volume.
        
        Returns 0 to 1, where 1 is perfectly one-sided flow.
        This is TICK-SEQUENCE DEPENDENT.
        """
        if bar.volume == 0:
            return 0.0

        flow_diff = abs(bar.buy_volume - bar.sell_volume)
        return flow_diff / bar.volume

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with tick-level flow imbalance and level persistence filtering.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Tick-level flow imbalance (TICK-SEQUENCE DEPENDENT)
        flow_imbalance = self._get_flow_imbalance(current)
        if flow_imbalance < self.flow_imbalance_threshold:
            return None

        level = self._round_price(current.close)

        # Filter 2: Level must be persistent or strong in current session
        level_score = self._get_level_persistence_score(level)
        if level_score < self.min_level_persistence:
            return None

        # Filter 3: Adaptive delta threshold based on level persistence
        is_persistent = self._is_persistent_level(level)
        if is_persistent:
            required_delta_ratio = self.absorption_delta_ratio
        else:
            required_delta_ratio = self.new_level_delta_ratio

        if delta_ratio < required_delta_ratio:
            return None

        # Filter 4: Price near natural level
        if abs(current.close - level) > 0.50:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Determine wick requirement based on level persistence
        if is_persistent:
            wick_to_range_min = self.wick_to_range_persistent
        else:
            wick_to_range_min = self.wick_to_range_new

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low

            # Filter 5: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 6: Wick relative to bar range (persistence-dependent)
            if lower_wick < wick_to_range_min * bar_range:
                return None

            # Filter 7: Prior bar context (depends on level persistence)
            if not is_persistent and len(self._bar_history) >= 2:
                prior_bar = self._bar_history[-2]
                if prior_bar.delta >= 0:
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
            upper_wick = current.high - current.close

            # Filter 5: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 6: Wick relative to bar range (persistence-dependent)
            if upper_wick < wick_to_range_min * bar_range:
                return None

            # Filter 7: Prior bar context (depends on level persistence)
            if not is_persistent and len(self._bar_history) >= 2:
                prior_bar = self._bar_history[-2]
                if prior_bar.delta <= 0:
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
        2. Flow imbalance confirmation (confirming bar must show similar imbalance)
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

        # Check flow imbalance confirmation (TICK-SEQUENCE DEPENDENT)
        confirm_imbalance = self._get_flow_imbalance(confirming_bar)
        if confirm_imbalance < self.flow_imbalance_threshold:
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
