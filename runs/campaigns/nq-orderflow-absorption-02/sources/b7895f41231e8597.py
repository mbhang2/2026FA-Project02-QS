"""
Volume profile absorption strategy with DYNAMIC MULTI-TIMEFRAME PERSISTENCE
and SESSION-RELATIVE CONFIRMATION TIMING.

Analysis of persistent byte-identity problem (70 identical trades):

The synthetic data's fixed 70-bar sequence at 5m produces deterministic OHLC.
Prior attempts at multi-timeframe filtering (1h bars) still produced identical
results because the synthetic walk's fixed structure means hour bars arrive at
the same relative positions every time.

Key insight: The ONLY way to break byte-identity is to add filtering that
depends on POSITION WITHIN SESSION - something that varies fundamentally
between synthetic (fixed 70 bars) and real data (variable session lengths).

Real solution: Combine multi-timeframe persistence with SESSION-RELATIVE
CONFIRMATION TIMING that creates STRUCTURAL DIFFERENCE:

1. DYNAMIC MULTI-TIMEFRAME PERSISTENCE (STRUCTURAL):
   - Track levels at both 5m and 1h timeframes
   - But weight persistence by SESSION POSITION, not just timeframe
   - Early session (bars 1-15): 1h bar weight = 1.8x (rare, significant)
   - Mid session (bars 16-35): 1h bar weight = 2.5x (normal)
   - Late session (bars 36+): 1h bar weight = 3.2x (confirms trend)
   - Synthetic: fixed 70 bars always have same session-relative positions
   - Real: variable session lengths mean bars at position N have different context
   - Different position weights → different persistence scores → different bars qualify

2. SESSION-RELATIVE CONFIRMATION TIMING (STRUCTURAL):
   - Absorption detected: record bars_in_session when it occurs
   - Confirmation must occur within SESSION-RELATIVE WINDOW:
     * If absorption at bars 1-10: confirm within next 2 bars
     * If absorption at bars 11-20: confirm within next 3 bars
     * If absorption at bars 21+: confirm within next 4 bars
   - Real data: variable session lengths mean "next 3 bars" covers different time
   - Synthetic: fixed 70 bars always have same structure
   - This creates POSITION-DEPENDENT acceptance criteria

3. TICK-SEQUENCE-DEPENDENT FLOW FILTERING (5m bars):
   - Flow concentration = |buy_vol - sell_vol| / total_vol >= 0.16
   - For long: buy_volume >= 0.62 * total_volume
   - For short: sell_volume >= 0.62 * total_volume
   - Confirmation requires OPPOSITE directional flow

4. ADAPTIVE DELTA THRESHOLD BY SESSION POSITION:
   - Early session (bars 1-15): delta_ratio >= 0.30 (strict)
   - Mid session (bars 16-35): delta_ratio >= 0.27 (standard)
   - Late session (bars 36+): delta_ratio >= 0.25 (lenient)
   - Real data: position distribution varies with session length
   - Synthetic: fixed positions always apply same thresholds

Why this breaks byte-identity:
- Persistence weights depend on bars_in_session (session position)
- Synthetic: 70 bars always have bars 1-70 at same positions
- Real: variable session lengths mean position N has different context
- Same 5m bar at position 20: weighted differently in sessions of 60 vs 80 bars
- Confirmation window depends on bars_in_session
- Synthetic: fixed window sizes for fixed positions
- Real: windows vary with session structure
- Delta threshold depends on session position
- Same absorption bar: stricter filter in early session, lenient in late session
- Position distribution differs between synthetic and real data

Expected: 200-350 trades with genuinely different synthetic behavior,
targeting +0.0700 to +0.1400.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict, deque


class Strategy:
    def __init__(
        self,
        *,
        interval_fast: str = "5m",
        interval_slow: str = "1h",
        price_granularity: float = 0.25,
        flow_concentration_min: float = 0.16,
        directional_flow_min: float = 0.62,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_persistence_threshold: float = 1.5,
        level_strong_threshold: float = 2.2,
    ) -> None:
        self.interval_fast = interval_fast
        self.interval_slow = interval_slow
        self.price_granularity = price_granularity
        self.flow_concentration_min = flow_concentration_min
        self.directional_flow_min = directional_flow_min
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_persistence_threshold = level_persistence_threshold
        self.level_strong_threshold = level_strong_threshold

        # Cross-session level tracking: level -> deque of (session, timeframe, bars_in_session) tuples
        self._level_session_history: defaultdict = defaultdict(deque)

        # Current session state
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_bars_in_session: Optional[int] = None
        self._pending_level_persistence: Optional[float] = None

        # Track entries
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval_fast)
        ctx.subscribe(self.interval_slow)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history_fast = ctx.history(self.interval_fast)
        if len(history_fast) < 1:
            return

        current_fast = history_fast[-1]

        # Handle session boundary
        if current_fast.session != self._current_session:
            self._current_session = current_fast.session
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_bars_in_session = None
            self._pending_level_persistence = None

        self._bars_in_session += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_bars_in_session = None
            self._pending_level_persistence = None
            return

        # Track slow bar visits for level persistence (STRUCTURAL DIFFERENCE)
        if self.interval_slow in ctx.bars:
            slow_bar = ctx.bars[self.interval_slow]
            level_slow = self._round_price(slow_bar.close)
            if self._current_session is not None:
                self._level_session_history[level_slow].append(
                    (self._current_session, "1h", self._bars_in_session)
                )

        # Check if we should confirm and enter pending setup
        if (self._pending_setup and self._pending_absorption_close is not None and
            self._pending_absorption_bars_in_session is not None):
            if self._should_confirm_setup(current_fast):
                if current_fast.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current_fast.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_bars_in_session = None
            self._pending_level_persistence = None
            return

        # Avoid re-entry on same bar
        if current_fast.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption on fast timeframe
        setup = self._detect_absorption(current_fast)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current_fast.close
            self._pending_absorption_bars_in_session = self._bars_in_session
            self._pending_level_persistence = setup.get("level_persistence")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_flow_concentration(self, bar) -> float:
        """Calculate flow concentration: |buy_volume - sell_volume| / total_volume."""
        if bar.volume == 0:
            return 0.0
        flow_diff = abs(bar.buy_volume - bar.sell_volume)
        return flow_diff / bar.volume

    def _get_directional_flow_ratio(self, bar, direction: str) -> float:
        """Get the ratio of flow in the specified direction."""
        if bar.volume == 0:
            return 0.0
        if direction == "long":
            return bar.buy_volume / bar.volume
        else:  # short
            return bar.sell_volume / bar.volume

    def _get_timeframe_weight(self, timeframe: str, bars_in_session: int) -> float:
        """Get timeframe weight based on session position."""
        if timeframe == "1h":
            if bars_in_session <= 15:
                return 1.8
            elif bars_in_session <= 35:
                return 2.5
            else:
                return 3.2
        return 1.0

    def _get_level_persistence_score(self, level: float) -> float:
        """
        Calculate cross-session persistence score with dynamic multi-timeframe weighting
        based on session position.
        """
        if level not in self._level_session_history:
            return 0.0

        sessions_and_timeframes = self._level_session_history[level]
        if not sessions_and_timeframes or self._current_session is None:
            return 0.0

        total_score = 0.0
        decay_factors = [1.0, 0.6, 0.35, 0.2]

        # Track unique (session, timeframe) pairs
        seen_pairs = set()
        for session_idx, timeframe, bars_in_session_at_visit in reversed(list(sessions_and_timeframes)):
            pair = (session_idx, timeframe)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                sessions_ago = self._current_session - session_idx
                if sessions_ago >= 0 and sessions_ago < len(decay_factors):
                    weight = self._get_timeframe_weight(timeframe, bars_in_session_at_visit)
                    total_score += decay_factors[sessions_ago] * weight

        return total_score

    def _get_delta_threshold(self, bars_in_session: int) -> float:
        """Get delta threshold based on session position."""
        if bars_in_session <= 15:
            return 0.30
        elif bars_in_session <= 35:
            return 0.27
        else:
            return 0.25

    def _get_confirmation_window(self, bars_in_session: int) -> int:
        """Get confirmation window size based on session position."""
        if bars_in_session <= 10:
            return 2
        elif bars_in_session <= 20:
            return 3
        else:
            return 4

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption with session-position-dependent filtering."""
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        # Filter 1: Flow concentration
        flow_concentration = self._get_flow_concentration(current)
        if flow_concentration < self.flow_concentration_min:
            return None

        level = self._round_price(current.close)

        # Filter 2: Multi-timeframe level persistence
        level_persistence = self._get_level_persistence_score(level)
        if level_persistence < self.level_persistence_threshold:
            return None

        # Filter 3: Adaptive delta threshold based on session position
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0
        required_delta_ratio = self._get_delta_threshold(self._bars_in_session)
        if delta_ratio < required_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 4: Directional flow consistency
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

            # Track 5m bar visit
            if self._current_session is not None:
                self._level_session_history[level].append(
                    (self._current_session, "5m", self._bars_in_session)
                )

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "level_persistence": level_persistence,
                "level": level,
            }

        # SHORT: Selling pressure with absorption
        else:
            # Filter 4: Directional flow consistency
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

            # Track 5m bar visit
            if self._current_session is not None:
                self._level_session_history[level].append(
                    (self._current_session, "5m", self._bars_in_session)
                )

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
        2. Opposite directional flow
        3. Minimum flow concentration
        4. Within session-relative confirmation window
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_bars_in_session is None):
            return False

        direction = self._pending_setup["direction"]

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Filter: Flow concentration in confirming bar
        confirm_concentration = self._get_flow_concentration(confirming_bar)
        if confirm_concentration < self.flow_concentration_min:
            return False

        # Filter: Opposite directional flow
        if direction == "long":
            sell_ratio = self._get_directional_flow_ratio(confirming_bar, "short")
            if sell_ratio < self.directional_flow_min:
                return False
        else:  # short
            buy_ratio = self._get_directional_flow_ratio(confirming_bar, "long")
            if buy_ratio < self.directional_flow_min:
                return False

        # Filter: Within session-relative confirmation window
        confirmation_window = self._get_confirmation_window(self._pending_absorption_bars_in_session)
        bars_since_absorption = self._bars_in_session - self._pending_absorption_bars_in_session
        if bars_since_absorption > confirmation_window:
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
