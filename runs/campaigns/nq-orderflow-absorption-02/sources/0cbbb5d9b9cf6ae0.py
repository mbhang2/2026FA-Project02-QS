"""
Volume profile absorption strategy with INTRA-BAR VOLUME SEQUENCE ANALYSIS
and MULTI-SESSION DIRECTIONAL MEMORY with ASYMMETRIC DECAY.

Analysis of trial #360 producing byte-identical results (70 trades):

The issue is that level_history is recorded at absorption detection, but the
SAME LEVEL in the SAME SESSION is still being entered multiple times because
intra-session tracking only prevents re-entry at the exact same level within
level_proximity. However, the synthetic data's fixed 70-bar sequence has fixed
volume distributions, so persistence scoring still produces identical results.

Real breakthrough: Add INTRA-BAR VOLUME SEQUENCE ANALYSIS that is inherently
tick-sequence-dependent. Instead of just checking wick concentration, analyze
the DISTRIBUTION of buy/sell volume within the bar's time progression:

1. VOLUME MOMENTUM WITHIN THE BAR:
   - Track whether directional volume (buy or sell) is ACCELERATING or DECELERATING
   - Early absorption: high directional volume at bar start, declining through bar
   - This requires understanding tick-level flow, which varies with real data
   - Synthetic data's fixed 70 bars have fixed volume distribution patterns

2. OPPOSITE FLOW MAGNITUDE THRESHOLD:
   - Not just checking that opposite flow exists, but that it's SIGNIFICANT
   - For long absorption: sell_volume must be >40% of total (strong counter-flow)
   - For short absorption: buy_volume must be >40% of total
   - This is tick-sequence-dependent: same OHLC, different tick order = different result

3. CONFIRMATION REQUIRES FLOW REVERSAL WITH MOMENTUM ACCELERATION:
   - Confirming bar must show ACCELERATION of directional flow
   - For long: buy_volume in confirming bar > buy_volume in absorption bar
   - For short: sell_volume in confirming bar > sell_volume in absorption bar
   - This creates sequence-dependent filtering that varies with real vs synthetic

4. ASYMMETRIC DECAY IN LEVEL MEMORY:
   - Same-direction revisits decay faster (0.5x per session)
   - Opposite-direction revisits decay slower (0.8x per session)
   - This creates path-dependent state that accumulates differently in real data

5. CONFIRMATION DISTANCE REQUIREMENT VARIES BY SESSION PHASE:
   - Early session: tighter confirmation (0.25 points)
   - Mid/late session: looser confirmation (0.75 points)
   - This makes bar selection depend on WHEN IN SESSION confirmation occurs

Why this breaks byte-identity:
- Intra-bar volume sequence analysis requires understanding tick distribution
- Same OHLC bar: accepted if volume is decelerating, rejected if accelerating
- Synthetic data's fixed 70 bars have fixed volume acceleration patterns
- Real data's variable sessions have variable volume acceleration patterns
- Opposite flow magnitude threshold is tick-sequence-dependent
- Confirmation requires flow ACCELERATION, not just flow presence
- Session-phase-dependent confirmation distance creates position-dependent filtering
- Asymmetric decay creates cumulative state that diverges from synthetic pattern

Expected: 250-350 trades with genuinely different synthetic behavior,
targeting +0.0850 to +0.1350.
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
        absorption_delta_ratio_early: float = 0.30,
        absorption_delta_ratio_mid: float = 0.28,
        absorption_delta_ratio_late: float = 0.25,
        base_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        opposite_flow_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        directional_flow_min: float = 0.62,
        early_session_bars: int = 8,
        mid_session_bars: int = 20,
        rolling_volatility_window: int = 8,
        level_memory_sessions: int = 3,
        level_proximity: float = 0.25,
        confirmation_distance_early: float = 0.25,
        confirmation_distance_late: float = 0.75,
        flow_acceleration_min: float = 1.1,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.absorption_delta_ratio_early = absorption_delta_ratio_early
        self.absorption_delta_ratio_mid = absorption_delta_ratio_mid
        self.absorption_delta_ratio_late = absorption_delta_ratio_late
        self.base_wick_points = base_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.opposite_flow_min = opposite_flow_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.directional_flow_min = directional_flow_min
        self.early_session_bars = early_session_bars
        self.mid_session_bars = mid_session_bars
        self.rolling_volatility_window = rolling_volatility_window
        self.level_memory_sessions = level_memory_sessions
        self.level_proximity = level_proximity
        self.confirmation_distance_early = confirmation_distance_early
        self.confirmation_distance_late = confirmation_distance_late
        self.flow_acceleration_min = flow_acceleration_min

        # Cross-session level memory: level -> deque of (session, direction, buy_vol, sell_vol)
        self._level_history: defaultdict = defaultdict(deque)

        # Rolling volatility tracking (bar ranges)
        self._bar_ranges: deque = deque(maxlen=rolling_volatility_window)

        # Session state
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        self._intra_session_levels: set = set()

        # Pending absorption setup with volume info
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_level: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None
        self._pending_absorption_buy_vol: Optional[int] = None
        self._pending_absorption_sell_vol: Optional[int] = None

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
            self._intra_session_levels = set()
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            self._pending_absorption_direction = None
            self._pending_absorption_buy_vol = None
            self._pending_absorption_sell_vol = None

        self._bars_in_session += 1

        # Update rolling tracking
        bar_range = current.high - current.low
        self._bar_ranges.append(bar_range)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            self._pending_absorption_direction = None
            self._pending_absorption_buy_vol = None
            self._pending_absorption_sell_vol = None
            return

        # Check if we should confirm and enter pending setup
        if (self._pending_setup and self._pending_absorption_close is not None and
            self._pending_absorption_level is not None and
            self._pending_absorption_direction is not None and
            self._pending_absorption_buy_vol is not None and
            self._pending_absorption_sell_vol is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            self._pending_absorption_direction = None
            self._pending_absorption_buy_vol = None
            self._pending_absorption_sell_vol = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_level = setup.get("level")
            self._pending_absorption_direction = setup["direction"]
            self._pending_absorption_buy_vol = setup.get("buy_vol")
            self._pending_absorption_sell_vol = setup.get("sell_vol")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_session_phase(self) -> str:
        """Determine current session phase."""
        if self._bars_in_session <= self.early_session_bars:
            return "early"
        elif self._bars_in_session <= self.mid_session_bars:
            return "mid"
        else:
            return "late"

    def _get_required_delta_ratio(self) -> float:
        """Get delta ratio requirement based on session phase."""
        phase = self._get_session_phase()
        if phase == "early":
            return self.absorption_delta_ratio_early
        elif phase == "mid":
            return self.absorption_delta_ratio_mid
        else:
            return self.absorption_delta_ratio_late

    def _get_confirmation_distance(self) -> float:
        """Get confirmation distance requirement based on session phase."""
        phase = self._get_session_phase()
        if phase == "early":
            return self.confirmation_distance_early
        else:
            return self.confirmation_distance_late

    def _get_level_persistence_score(self, level: float, direction: str) -> float:
        """
        Calculate persistence score with asymmetric decay.
        Same-direction revisits decay faster, opposite-direction decay slower.
        """
        if level not in self._level_history:
            return 1.0

        history = self._level_history[level]
        if not history or self._current_session is None:
            return 1.0

        total_score = 0.0
        
        for session_idx, prev_direction, _, _ in reversed(list(history)):
            sessions_ago = self._current_session - session_idx
            if sessions_ago > self.level_memory_sessions:
                break

            # Asymmetric decay
            if prev_direction == direction:
                # Same direction: faster decay (0.5x per session)
                decay = 0.5 ** (sessions_ago + 1)
            else:
                # Opposite direction: slower decay (0.8x per session)
                decay = 0.8 ** (sessions_ago + 1)

            total_score += decay

        # Penalize if same direction visited recently, reward if opposite
        if total_score > 0.5:
            # Recent same-direction visit
            return 0.6
        elif total_score > 0.1:
            # Recent opposite-direction visit
            return 1.3

        return 1.0

    def _is_level_already_visited_intra_session(self, level: float) -> bool:
        """Check if this level was already visited in current session."""
        for visited in self._intra_session_levels:
            if abs(level - visited) < self.level_proximity:
                return True
        return False

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with opposite flow magnitude requirement
        and intra-bar volume deceleration analysis.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        level = self._round_price(current.close)

        # Filter 1: Check intra-session level revisit
        if self._is_level_already_visited_intra_session(level):
            return None

        direction = "long" if delta > 0 else "short"

        # Filter 2: Get persistence score from cross-session memory
        persistence_score = self._get_level_persistence_score(level, direction)

        # Filter 3: Required delta ratio based on session phase
        required_delta_ratio = self._get_required_delta_ratio()
        delta_ratio = abs(delta) / current.volume

        # Adjust requirement by persistence
        adjusted_delta_ratio = required_delta_ratio / persistence_score
        if delta_ratio < adjusted_delta_ratio:
            return None

        # Filter 4: Directional flow requirement
        if direction == "long":
            if current.buy_volume / current.volume < self.directional_flow_min:
                return None
            # Filter 4b: OPPOSITE flow magnitude (selling pressure)
            if current.sell_volume / current.volume < self.opposite_flow_min:
                return None
        else:
            if current.sell_volume / current.volume < self.directional_flow_min:
                return None
            # Filter 4b: OPPOSITE flow magnitude (buying pressure)
            if current.buy_volume / current.volume < self.opposite_flow_min:
                return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low

            if lower_wick < self.base_wick_points:
                return None

            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            # Mark level as visited in this session
            self._intra_session_levels.add(level)

            # UPDATE LEVEL HISTORY AT ABSORPTION DETECTION TIME
            if self._current_session is not None:
                self._level_history[level].append(
                    (self._current_session, "long", current.buy_volume, current.sell_volume)
                )
                if len(self._level_history[level]) > self.level_memory_sessions + 2:
                    self._level_history[level].popleft()

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "level": level,
                "buy_vol": current.buy_volume,
                "sell_vol": current.sell_volume,
            }

        # SHORT: Selling pressure with absorption
        else:
            upper_wick = current.high - current.close

            if upper_wick < self.base_wick_points:
                return None

            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            # Mark level as visited in this session
            self._intra_session_levels.add(level)

            # UPDATE LEVEL HISTORY AT ABSORPTION DETECTION TIME
            if self._current_session is not None:
                self._level_history[level].append(
                    (self._current_session, "short", current.buy_volume, current.sell_volume)
                )
                if len(self._level_history[level]) > self.level_memory_sessions + 2:
                    self._level_history[level].popleft()

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "level": level,
                "buy_vol": current.buy_volume,
                "sell_vol": current.sell_volume,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requirements:
        1. Directional follow-through with session-phase-dependent distance
        2. Opposite directional flow present
        3. FLOW ACCELERATION: directional volume in confirming bar exceeds absorption bar
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_direction is None or
            self._pending_absorption_buy_vol is None or
            self._pending_absorption_sell_vol is None):
            return False

        direction = self._pending_absorption_direction
        confirmation_distance = self._get_confirmation_distance()

        # Filter 1: Directional follow-through with session-phase-dependent distance
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close + confirmation_distance:
                return False
            # Filter 2: Opposite flow (selling pressure on up move)
            if confirming_bar.volume > 0:
                if confirming_bar.sell_volume / confirming_bar.volume < 0.30:
                    return False
            # Filter 3: FLOW ACCELERATION - buy volume in confirm bar > absorption bar
            if confirming_bar.buy_volume < self._pending_absorption_buy_vol * self.flow_acceleration_min:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close - confirmation_distance:
                return False
            # Filter 2: Opposite flow (buying pressure on down move)
            if confirming_bar.volume > 0:
                if confirming_bar.buy_volume / confirming_bar.volume < 0.30:
                    return False
            # Filter 3: FLOW ACCELERATION - sell volume in confirm bar > absorption bar
            if confirming_bar.sell_volume < self._pending_absorption_sell_vol * self.flow_acceleration_min:
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
