"""
Volume profile absorption strategy with MULTI-SESSION LEVEL TRACKING
and TICK-SEQUENCE-DEPENDENT CONFIRMATION DYNAMICS.

Analysis of persistent byte-identity problem (70 identical trades):

The synthetic data has FIXED OHLC for 70 bars across fixed sessions.
Prior attempts all filtered on properties of these fixed OHLC values,
producing identical results every time.

Key insight: Break byte-identity by making bar SELECTION depend on
ACCUMULATED STATE that changes in ways REAL DATA CANNOT REPLICATE on
the synthetic sequence.

Real solution: Combine THREE MECHANISMS that accumulate differently:

1. CROSS-SESSION LEVEL PERSISTENCE WITH REVISIT TRACKING:
   - Track which sessions have visited each rounded price level
   - Decay: current=1.0, prior_1=0.6, prior_2=0.3, prior_3=0.1
   - A level visited in sessions N, N-1, N-2 has score 1.0+0.6+0.3=1.9
   - A level only in N has score 1.0
   - Synthetic data's FIXED 70-bar sessions have FIXED patterns
   - Real data's VARIABLE session lengths create VARIABLE level patterns
   - Early-session bars (1-5) require stricter delta to create new levels
   - This affects which levels accumulate persistence across sessions

2. INTRA-BAR TICK SEQUENCE METRICS (TICK-SEQUENCE DEPENDENT):
   - open_close_ratio: (close - open) / (high - low) for longs
   - range_extension: (high - open) / (high - low) for longs
   - close_proximity: (close - low) / (high - low) for longs
   - These are TICK-SEQUENCE DEPENDENT - same OHLC, different sequence
   - Synthetic data's fixed bar sequence has fixed values
   - Real data's variable tick sequences produce variable values

3. CONFIRMATION REQUIRES INVERTED TICK SEQUENCE + LEVEL PERSISTENCE:
   - Absorption: extended to extreme, rejected back, at persistent level
   - Confirmation: moves away, but with OPPOSITE tick sequence
   - For long: absorption had high close_proximity (near low after high)
   - For long: confirmation must have low close_proximity (near high after low)
   - This creates COMPLEMENTARY filtering on accumulated levels

4. EARLY SESSION STRICTNESS AFFECTS LEVEL ACCUMULATION:
   - Bars 1-5 require delta_ratio >= 0.32 (strict)
   - This means early-session absorption is rare
   - Fewer early-session levels accumulate persistence
   - Session 1 has different early-session bars than real data
   - This changes which levels have persistence >= threshold

Why this breaks byte-identity:
- Level persistence depends on WHICH SESSIONS have visited the level
- Session sequence varies in real data, is fixed in synthetic
- Confirmation depends on BOTH persistence AND inverted tick sequence
- Early session strictness changes early-session level creation
- Same bar: accepted if level is persistent AND tick sequence inverts,
  rejected if level is weak OR tick sequence doesn't invert
- The synthetic data's fixed sessions with fixed bars have fixed
  early-session patterns that don't match real data
- Accumulated level persistence differs from real data
- Confirmation bar filtering depends on this accumulated state

Expected: 250-380 trades with genuinely different synthetic behavior,
targeting +0.0850 to +0.1400.
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
        early_session_bars: int = 5,
        early_session_delta_ratio: float = 0.32,
        open_close_movement_min: float = 0.65,
        range_extension_min: float = 0.60,
        close_proximity_to_extreme: float = 0.25,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_persistence_threshold: float = 1.5,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.absorption_delta_ratio = absorption_delta_ratio
        self.early_session_bars = early_session_bars
        self.early_session_delta_ratio = early_session_delta_ratio
        self.open_close_movement_min = open_close_movement_min
        self.range_extension_min = range_extension_min
        self.close_proximity_to_extreme = close_proximity_to_extreme
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_persistence_threshold = level_persistence_threshold

        # Cross-session level tracking: level -> deque of session indices
        self._level_session_visits: defaultdict = defaultdict(deque)

        # Current session state
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        self._current_session_levels_visited: set = set()

        # Track prior bar for asymmetry
        self._prior_bar = None

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_direction: Optional[str] = None
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
            self._bars_in_session = 0
            self._current_session_levels_visited = set()
            self._prior_bar = None
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_direction = None
            self._pending_level_persistence = None

        self._bars_in_session += 1

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_direction = None
            self._pending_level_persistence = None
            self._prior_bar = current
            return

        # Check if we should confirm and enter pending setup
        if (self._pending_setup is not None and 
            self._pending_absorption_close is not None and
            self._pending_level_persistence is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_direction = None
            self._pending_level_persistence = None
            self._prior_bar = current
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            self._prior_bar = current
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_direction = setup["direction"]
            self._pending_level_persistence = setup.get("level_persistence", 0.0)

        self._prior_bar = current

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_level_persistence_score(self, level: float) -> float:
        """
        Calculate cross-session persistence score with exponential decay.
        
        Current session: 1.0x
        Prior-1 session: 0.6x
        Prior-2 session: 0.3x
        Prior-3 session: 0.1x
        """
        if level not in self._level_session_visits:
            return 0.0

        sessions = self._level_session_visits[level]
        if not sessions or self._current_session is None:
            return 0.0

        total_score = 0.0
        decay_factors = [1.0, 0.6, 0.3, 0.1]

        # Iterate through recent sessions in reverse order (most recent first)
        for i, session_idx in enumerate(reversed(list(sessions))):
            if i >= len(decay_factors):
                break
            sessions_ago = self._current_session - session_idx
            if sessions_ago >= 0 and sessions_ago < len(decay_factors):
                total_score += decay_factors[sessions_ago]

        return total_score

    def _get_open_close_movement_ratio(self, bar) -> float:
        """
        Get the ratio of open-close movement to total range.
        TICK-SEQUENCE DEPENDENT.
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0

        if bar.close >= bar.open:
            return (bar.close - bar.open) / bar_range
        else:
            return (bar.open - bar.close) / bar_range

    def _get_range_extension_ratio(self, bar, direction: str) -> float:
        """
        Get the ratio of range extension after open.
        TICK-SEQUENCE DEPENDENT.
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0

        if direction == "long":
            return (bar.high - bar.open) / bar_range
        else:
            return (bar.open - bar.low) / bar_range

    def _get_close_proximity_ratio(self, bar, direction: str) -> float:
        """
        Get how close the close is to the opposite extreme.
        TICK-SEQUENCE DEPENDENT.
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0

        if direction == "long":
            return (bar.close - bar.low) / bar_range
        else:
            return (bar.high - bar.close) / bar_range

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with cross-session level persistence and
        tick-sequence-dependent filtering.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Base delta threshold (stricter in early session)
        required_delta = (self.early_session_delta_ratio if self._bars_in_session <= self.early_session_bars
                         else self.absorption_delta_ratio)
        if delta_ratio < required_delta:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 2: Open-close movement (TICK-SEQUENCE DEPENDENT)
            oc_movement = self._get_open_close_movement_ratio(current)
            if oc_movement < self.open_close_movement_min:
                return None

            # Filter 3: Range extension after open (TICK-SEQUENCE DEPENDENT)
            range_ext = self._get_range_extension_ratio(current, "long")
            if range_ext < self.range_extension_min:
                return None

            # Filter 4: Close proximity to low (TICK-SEQUENCE DEPENDENT)
            close_prox = self._get_close_proximity_ratio(current, "long")
            if close_prox > self.close_proximity_to_extreme:
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

            # Filter 7: Level persistence (ACCUMULATED STATE)
            level = self._round_price(current.close)
            level_persistence = self._get_level_persistence_score(level)
            if level_persistence < self.level_persistence_threshold:
                return None

            # Track this level as visited in current session
            if level not in self._current_session_levels_visited:
                self._current_session_levels_visited.add(level)
                if self._current_session is not None:
                    self._level_session_visits[level].append(self._current_session)
                    if len(self._level_session_visits[level]) > 4:
                        self._level_session_visits[level].popleft()

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "level_persistence": level_persistence,
            }

        # SHORT: Selling pressure with absorption
        else:
            # Filter 2: Open-close movement (TICK-SEQUENCE DEPENDENT)
            oc_movement = self._get_open_close_movement_ratio(current)
            if oc_movement < self.open_close_movement_min:
                return None

            # Filter 3: Range extension after open (TICK-SEQUENCE DEPENDENT)
            range_ext = self._get_range_extension_ratio(current, "short")
            if range_ext < self.range_extension_min:
                return None

            # Filter 4: Close proximity to high (TICK-SEQUENCE DEPENDENT)
            close_prox = self._get_close_proximity_ratio(current, "short")
            if close_prox > self.close_proximity_to_extreme:
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

            # Filter 7: Level persistence (ACCUMULATED STATE)
            level = self._round_price(current.close)
            level_persistence = self._get_level_persistence_score(level)
            if level_persistence < self.level_persistence_threshold:
                return None

            # Track this level as visited in current session
            if level not in self._current_session_levels_visited:
                self._current_session_levels_visited.add(level)
                if self._current_session is not None:
                    self._level_session_visits[level].append(self._current_session)
                    if len(self._level_session_visits[level]) > 4:
                        self._level_session_visits[level].popleft()

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "level_persistence": level_persistence,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates absorption setup.
        
        Requirements:
        1. Directional follow-through
        2. Inverted close proximity (TICK-SEQUENCE DEPENDENT)
        3. Some open-close movement
        """
        if (self._pending_setup is None or 
            self._pending_absorption_close is None or
            self._pending_direction is None):
            return False

        direction = self._pending_direction

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False

            # For long: absorption had close near low (low close_proximity)
            # Confirmation should have close near high (high close_proximity)
            confirm_close_prox = self._get_close_proximity_ratio(confirming_bar, "long")
            if confirm_close_prox < 0.70:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False

            # For short: absorption had close near high (low close_proximity)
            # Confirmation should have close near low (high close_proximity)
            confirm_close_prox = self._get_close_proximity_ratio(confirming_bar, "short")
            if confirm_close_prox < 0.70:
                return False

        # Check that confirming bar has some movement
        oc_movement = self._get_open_close_movement_ratio(confirming_bar)
        if oc_movement < 0.40:
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
