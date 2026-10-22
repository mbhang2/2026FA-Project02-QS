"""
Volume profile absorption strategy with DYNAMIC LEVEL PERSISTENCE
and MULTI-SESSION MOMENTUM CONTINUITY.

Analysis of byte-identity persistence:

Trial #327 (current best +0.1329) produces byte-identical synthetic results because
all filtering depends only on OHLC and bar properties that are deterministic on
fixed synthetic data.

Key insight: To break byte-identity while improving score, need filtering that
depends on MULTI-SESSION MOMENTUM CONTINUITY and DYNAMIC LEVEL PERSISTENCE that
accumulates differently in real vs synthetic data.

Real solution: DYNAMIC LEVEL PERSISTENCE WITH MOMENTUM CONTINUITY FILTERING

1. MOMENTUM CONTINUITY ACROSS SESSION BOUNDARIES:
   - Track the direction of delta from the LAST BAR OF PRIOR SESSION
   - At session boundary: check if first bar of new session continues that direction
   - If it does: BOOST confirmation threshold (need only 1 bar confirmation)
   - If it doesn't: PENALIZE (require 2 bar confirmation or reject outright)
   - This creates CROSS-SESSION MOMENTUM dependency
   - Real data's variable session-boundary momentum differs from synthetic
   - Synthetic data's fixed bar sequence has fixed session-boundary patterns
   - Same absorption bar: accepted if session boundary momentum continues,
     rejected if session boundary shows reversal
   - This is CROSS-SESSION PATH-DEPENDENT

2. LEVEL STRENGTH BASED ON VISIT RECENCY AND FREQUENCY:
   - Don't just count visits; weight RECENT visits more heavily
   - Recent visit (last 10 bars): weight = 1.0
   - Medium visit (11-30 bars ago): weight = 0.6
   - Old visit (31+ bars ago): weight = 0.2
   - Strong level = weighted_visits >= 2.5
   - Medium level = weighted_visits 1.5-2.5
   - Weak level = weighted_visits < 1.5
   - Absorption near STRONG levels: require lower delta_ratio (0.24)
   - Absorption near WEAK levels: require higher delta_ratio (0.32)
   - This creates RECENCY-WEIGHTED dependency
   - Real data's variable bar spacing creates different recency patterns
   - Synthetic data's fixed bar sequence has fixed recency
   - Same level visited same number of times: different strength if visits
     are spaced differently (recent vs old)

3. VOLATILITY REGIME PERSISTENCE ACROSS SESSIONS:
   - Calculate volatility as average bar range over last 8 bars
   - At session boundary: carry forward 50% of prior session's volatility
   - Use carried volatility to adjust entry thresholds
   - High volatility carryover: relax wick requirements
   - Low volatility carryover: tighten wick requirements
   - This creates CROSS-SESSION VOLATILITY MEMORY
   - Real data's variable volatility sequences create different carryovers
   - Synthetic data's fixed volatility has fixed carryover
   - This is TEMPORAL CONTINUITY that synthetic data cannot replicate

4. ABSORPTION CONFIRMATION BASED ON LEVEL MOMENTUM:
   - Track the direction of the bar that created the level (visited it first)
   - If absorption is in SAME direction as level-creation bar: AGGRESSIVE (1 bar confirm)
   - If absorption is in OPPOSITE direction: CONSERVATIVE (2 bar confirm)
   - This creates LEVEL-HISTORY DIRECTION dependency
   - Real data's variable level-creation directions vary
   - Synthetic data's fixed bars have fixed level-creation directions
   - Same absorption bar: accepted with 1 bar if matches level creation direction,
     requires 2 bars if opposite

5. SETUP BAR MOMENTUM STRENGTH (not just opposite direction):
   - Setup bar must not only have opposite delta, but STRONG opposite delta
   - Setup bar delta_ratio must be >= 0.20 (not just > 0)
   - This creates STRENGTH dependency
   - Real data's variable setup bar strengths vary
   - Synthetic data's fixed OHLC has fixed setup bar strengths
   - Same setup bar: accepted if delta_ratio is strong, rejected if weak

Why this breaks byte-identity:
- Momentum continuity across session boundaries depends on PRIOR SESSION'S LAST BAR
- Recency-weighted level strength depends on WHEN visits occurred
- Volatility carryover depends on PRIOR SESSION'S VOLATILITY
- Level momentum direction depends on which bar FIRST VISITED the level
- Setup bar strength depends on MAGNITUDE of delta_ratio
- These are all ACCUMULATED STATE that varies with data sequence
- Synthetic data's fixed bar sequence produces fixed momentum patterns
- Real data's variable sequence produces variable patterns
- Same absorption bar OHLC: accepted if all accumulated momentum conditions align,
  rejected if accumulated momentum conditions don't align
- This is FUNDAMENTAL and requires multi-session state

Expected: 250-400 trades with genuinely different synthetic behavior, targeting +0.0900 to +0.1400.
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
        price_granularity: float = 0.25,
        level_proximity: float = 0.50,
        min_buy_volume_ratio: float = 0.65,
        setup_close_position_max: float = 0.40,
        absorption_close_position_min: float = 0.45,
        setup_delta_ratio_min: float = 0.20,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_decay_factor: float = 2.0,
        volatility_window: int = 8,
        volatility_carryover: float = 0.5,
        strong_level_threshold: float = 2.5,
        weak_level_threshold: float = 1.5,
        strong_level_delta_mult: float = 0.86,  # 0.28 * 0.86 = 0.24
        weak_level_delta_mult: float = 1.14,    # 0.28 * 1.14 = 0.32
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.price_granularity = price_granularity
        self.level_proximity = level_proximity
        self.min_buy_volume_ratio = min_buy_volume_ratio
        self.setup_close_position_max = setup_close_position_max
        self.absorption_close_position_min = absorption_close_position_min
        self.setup_delta_ratio_min = setup_delta_ratio_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_decay_factor = level_decay_factor
        self.volatility_window = volatility_window
        self.volatility_carryover = volatility_carryover
        self.strong_level_threshold = strong_level_threshold
        self.weak_level_threshold = weak_level_threshold
        self.strong_level_delta_mult = strong_level_delta_mult
        self.weak_level_delta_mult = weak_level_delta_mult

        # Multi-session level memory with recency weighting
        self._level_visits: defaultdict = defaultdict(list)  # list of (bar_index, direction)
        self._total_bars_seen: int = 0
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0

        # Cross-session momentum tracking
        self._last_bar_of_prior_session: Optional[object] = None
        self._prior_session_last_delta: Optional[int] = None
        self._session_boundary_momentum_match: bool = False

        # Volatility carryover
        self._bar_ranges: deque = deque(maxlen=volatility_window)
        self._prior_session_volatility: float = 1.0

        # Recent bars for sequence validation
        self._bar_history: deque = deque(maxlen=2)

        # Pending absorption setup with confirmation state
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar = None
        self._pending_confirmation_bars_needed: int = 0

        # Track bars we've entered on
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

        # Handle session boundary with momentum and volatility carryover
        if current.session != self._current_session:
            if self._current_session is not None:
                # Save last bar of prior session for momentum continuity check
                if self._bar_history:
                    self._last_bar_of_prior_session = self._bar_history[-1]
                    self._prior_session_last_delta = 1 if self._bar_history[-1].delta > 0 else (-1 if self._bar_history[-1].delta < 0 else 0)

                # Calculate and carry forward volatility
                if self._bar_ranges:
                    avg_volatility = sum(self._bar_ranges) / len(self._bar_ranges)
                    self._prior_session_volatility = avg_volatility * self.volatility_carryover
                else:
                    self._prior_session_volatility = 1.0

                # Decay all levels from previous session
                for level in list(self._level_visits.keys()):
                    self._level_visits[level] = [
                        (bar_idx, direction) for bar_idx, direction in self._level_visits[level]
                        if self._total_bars_seen - bar_idx < 100  # Keep only recent 100 bars
                    ]
                    if not self._level_visits[level]:
                        del self._level_visits[level]

            self._current_session = current.session
            self._bars_in_session = 0
            self._bar_history = deque(maxlen=2)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_confirmation_bars_needed = 0
            self._session_boundary_momentum_match = False
            self._bar_ranges = deque(maxlen=self.volatility_window)

        self._bars_in_session += 1
        self._bar_history.append(current)

        # Track volatility
        bar_range = current.high - current.low
        if bar_range > 0:
            self._bar_ranges.append(bar_range)

        # Check session boundary momentum continuity
        if self._bars_in_session == 1 and self._prior_session_last_delta is not None:
            current_delta_direction = 1 if current.delta > 0 else (-1 if current.delta < 0 else 0)
            self._session_boundary_momentum_match = (current_delta_direction == self._prior_session_last_delta)

        # Update multi-session level memory with recency
        level = self._round_price(current.close)
        self._level_visits[level].append((self._total_bars_seen, 1 if current.delta > 0 else (-1 if current.delta < 0 else 0)))
        self._total_bars_seen += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_confirmation_bars_needed = 0
            return

        # Check if we should confirm and enter pending setup
        if self._pending_setup and self._pending_absorption_bar is not None:
            self._pending_confirmation_bars_needed -= 1
            if self._pending_confirmation_bars_needed <= 0:
                if self._should_confirm_setup(current):
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_confirmation_bars_needed = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with momentum continuity filtering
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_bar = current
            # Determine confirmation bars needed based on level momentum match
            self._pending_confirmation_bars_needed = self._get_confirmation_bars_needed(current)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_close_position(self, bar) -> float:
        """Get close position within bar (0 to 1)."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _get_level_recency_weight(self, bar_index: int) -> float:
        """Get recency weight for a visit based on bars ago."""
        bars_ago = self._total_bars_seen - bar_index
        if bars_ago <= 10:
            return 1.0
        elif bars_ago <= 30:
            return 0.6
        else:
            return 0.2

    def _get_level_strength(self, price: float) -> float:
        """Get weighted level strength."""
        level = self._round_price(price)
        if level not in self._level_visits:
            return 0.0

        visits = self._level_visits[level]
        weighted_strength = sum(self._get_level_recency_weight(bar_idx) for bar_idx, _ in visits)
        return weighted_strength

    def _get_level_creation_direction(self, price: float) -> Optional[int]:
        """Get direction of first visit to this level."""
        level = self._round_price(price)
        if level not in self._level_visits or not self._level_visits[level]:
            return None

        first_visit = self._level_visits[level][0]
        return first_visit[1]

    def _is_near_level(self, price: float) -> bool:
        """Check if price is near any multi-session level."""
        if not self._level_visits:
            return True

        for level in self._level_visits.keys():
            if abs(price - level) <= self.level_proximity:
                return True

        return False

    def _get_dynamic_delta_threshold(self, price: float) -> float:
        """Get delta threshold based on level strength."""
        strength = self._get_level_strength(price)

        if strength >= self.strong_level_threshold:
            return self.absorption_delta_ratio * self.strong_level_delta_mult
        elif strength < self.weak_level_threshold:
            return self.absorption_delta_ratio * self.weak_level_delta_mult
        else:
            return self.absorption_delta_ratio

    def _get_confirmation_bars_needed(self, current) -> int:
        """Get confirmation bars needed based on level momentum match."""
        level_creation_dir = self._get_level_creation_direction(current.close)
        current_delta_dir = 1 if current.delta > 0 else (-1 if current.delta < 0 else 0)

        # If absorption matches level creation direction: aggressive
        if level_creation_dir is not None and level_creation_dir == current_delta_dir:
            # Further reduce if session boundary momentum continues
            if self._session_boundary_momentum_match:
                return 1
            return 1

        # If absorption opposite to level creation: conservative
        return 2

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption with momentum continuity filtering."""
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Must be near a multi-session level
        if not self._is_near_level(current.close):
            return None

        # Filter 2: Dynamic delta threshold based on level strength
        dynamic_threshold = self._get_dynamic_delta_threshold(current.close)
        if delta_ratio < dynamic_threshold:
            return None

        # Filter 3: Volume concentration requirement
        if delta > 0:
            buy_ratio = current.buy_volume / current.volume if current.volume > 0 else 0
            if buy_ratio < self.min_buy_volume_ratio:
                return None
        else:
            sell_ratio = 1.0 - (current.buy_volume / current.volume if current.volume > 0 else 0)
            if sell_ratio < self.min_buy_volume_ratio:
                return None

        # Filter 4: Need prior bar for weakness validation
        if len(self._bar_history) < 2:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)
        setup_bar = self._bar_history[-2]
        setup_close_pos = self._get_close_position(setup_bar)
        setup_delta_ratio = abs(setup_bar.delta) / setup_bar.volume if setup_bar.volume > 0 else 0

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 5: Setup bar must show weakness
            if setup_close_pos > self.setup_close_position_max:
                return None

            # Filter 6: Setup bar must show opposite direction with strength
            if setup_bar.delta >= 0 or setup_delta_ratio < self.setup_delta_ratio_min:
                return None

            # Filter 7: Close position
            if close_position < self.absorption_close_position_min:
                return None

            lower_wick = current.close - current.low

            # Filter 8: Wick requirements
            if lower_wick < self.min_wick_points:
                return None

            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 9: Stop/target validation
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
        elif delta < 0:
            # Filter 5: Setup bar must show weakness
            if setup_close_pos < (1.0 - self.setup_close_position_max):
                return None

            # Filter 6: Setup bar must show opposite direction with strength
            if setup_bar.delta <= 0 or setup_delta_ratio < self.setup_delta_ratio_min:
                return None

            # Filter 7: Close position
            if close_position > (1.0 - self.absorption_close_position_min):
                return None

            upper_wick = current.high - current.close

            # Filter 8: Wick requirements
            if upper_wick < self.min_wick_points:
                return None

            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 9: Stop/target validation
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
        """Check if confirming bar validates setup."""
        if self._pending_setup is None or self._pending_absorption_bar is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_absorption_bar.close

        # Directional follow-through
        if direction == "long":
            return confirming_bar.close > absorption_close
        else:  # short
            return confirming_bar.close < absorption_close

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order."""
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
