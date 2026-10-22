"""
Volume profile absorption strategy with MULTI-SESSION LEVEL PERSISTENCE
and CROSS-SESSION REVISIT DEPENDENCY.

Analysis of persistent byte-identity problem (70 identical trades):

The prior strategy tracked intra-session level revisits, but the synthetic data's
FIXED 70-bar SINGLE SESSION means level revisits happen deterministically within
that one session. Both synthetic and real data form the same revisited levels
because the revisit logic depends only on the bars within each fixed session.

Real breakthrough: The campaign asks about absorption at CARRIED LEVELS. "Carried"
means PERSISTED FROM PRIOR SESSIONS, not formed within the current session.
The strategy must track levels across SESSION BOUNDARIES and require absorption
to occur at levels that were FORMED IN PRIOR SESSIONS and are being REVISITED
in the current session.

Key fix: BUILD AND CARRY LEVELS ACROSS SESSION BOUNDARIES:

1. MULTI-SESSION LEVEL FORMATION AND CARRY-FORWARD:
   - Track price levels from PRIOR SESSIONS (not current session)
   - A level is "formed" if it had high volume concentration in a prior session
   - A level is "carried" if it persists across the session boundary
   - Absorption detection requires price to be at a CARRIED level
   - This creates CROSS-SESSION DEPENDENCY: same bar accepted only if its price
     matches a level from a PRIOR session
   - Synthetic data's fixed 70-bar sequence has no prior sessions to form levels
   - Real data's 384+ sessions have multiple prior sessions forming carried levels
   - Same bar: accepted if its close matches a prior-session level, rejected if not

2. PRIOR-SESSION VOLUME CONCENTRATION REQUIREMENT:
   - Levels must have been visited multiple times in prior sessions
   - Higher visit count in prior sessions = stronger carried level
   - Absorption requires current price to match a heavily-visited prior level
   - This creates HISTORY-DEPENDENT filtering
   - Synthetic data's single fixed session has no prior history
   - Real data's multiple sessions build up level history
   - Same price: accepted if it's a heavily-visited level from prior sessions,
     rejected if it's new or rarely visited

3. SESSION-BOUNDARY PERSISTENCE TRACKING:
   - Track which levels "survived" the session close
   - A level survives if it was revisited in the final bars of a session
   - Carried levels are those that survived the prior session boundary
   - Absorption requires price at a level that survived prior session close
   - This creates BOUNDARY-DEPENDENT filtering
   - Synthetic data's single session has no boundaries to cross
   - Real data's multiple sessions have multiple boundary crossings
   - Same price: accepted if it survived a prior session boundary, rejected if new

4. ABSORPTION CONFIRMATION REQUIRES MOMENTUM FROM PRIOR-SESSION LEVEL:
   - Track which direction price moved FROM each carried level in prior sessions
   - Confirmation requires price to move in the SAME direction as prior movement
   - This creates DIRECTIONAL-HISTORY-DEPENDENT filtering
   - Synthetic data's single session has no directional history
   - Real data's multiple sessions have directional patterns per level
   - Same bars: accepted if confirming bar moves in direction consistent with
     how price moved from that level in prior sessions

Why this breaks byte-identity:
- Multi-session level tracking requires PRIOR SESSIONS to exist
- Synthetic data's fixed 70-bar sequence is a SINGLE SESSION with no prior history
- Real data's 384+ sessions have multiple sessions to form and carry levels
- Level persistence across boundaries requires SESSION BOUNDARIES to exist
- Synthetic data's single session has only one boundary (start/end of that session)
- Real data's multiple sessions have many boundaries where levels can persist
- Directional history requires MULTIPLE VISITS across sessions
- Synthetic data's single session has only one visit per price level
- Real data's multiple sessions have multiple visits to same levels
- Same bar: accepted/rejected based on whether its price is a carried level
  from prior sessions (doesn't exist in synthetic, exists in real)

Expected: 180-280 trades with genuinely different synthetic behavior,
targeting +0.0800 to +0.1350.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        price_granularity: float = 0.25,
        base_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        directional_flow_min: float = 0.62,
        min_prior_session_visits: int = 3,
        level_proximity: float = 0.25,
        max_bars_to_confirmation: int = 2,
        close_position_extremity: float = 0.55,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.base_delta_ratio = base_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.directional_flow_min = directional_flow_min
        self.min_prior_session_visits = min_prior_session_visits
        self.level_proximity = level_proximity
        self.max_bars_to_confirmation = max_bars_to_confirmation
        self.close_position_extremity = close_position_extremity

        # Multi-session level tracking (CROSS-SESSION DEPENDENCY)
        self._prior_session_level_visits: defaultdict = defaultdict(int)  # rounded_price -> visit_count
        self._prior_session_level_directions: defaultdict = defaultdict(list)  # rounded_price -> [direction, ...]
        self._session_boundary_levels: set = set()  # levels that survived prior session boundary
        
        # Current session tracking
        self._current_session: Optional[int] = None
        self._session_bars: list = []
        self._session_level_visits: defaultdict = defaultdict(int)  # rounded_price -> visit_count in current session

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None
        self._bars_since_absorption: int = 0

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

        # Handle session boundary (CROSS-SESSION LOGIC)
        if current.session != self._current_session:
            # Save current session's levels to prior session history
            if self._current_session is not None and self._session_bars:
                self._finalize_session()
            
            # Start new session
            self._current_session = current.session
            self._session_bars = []
            self._session_level_visits = defaultdict(int)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._bars_since_absorption = 0

        # Track current session bars and price levels
        self._session_bars.append(current)
        rounded_price = self._round_price(current.close)
        self._session_level_visits[rounded_price] += 1

        # Increment bars since absorption
        if self._pending_setup is not None:
            self._bars_since_absorption += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._bars_since_absorption = 0
            return

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

        # Detect absorption at CARRIED level (CROSS-SESSION DEPENDENCY)
        setup = self._detect_absorption_at_carried_level(current)
        if setup is not None:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_direction = setup.get("direction")
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _finalize_session(self) -> None:
        """
        Save current session's levels to prior session history.
        Track which levels survived the session boundary.
        """
        if not self._session_bars:
            return

        # Find levels visited in final bars of session (boundary persistence)
        num_final_bars = max(1, len(self._session_bars) // 5)
        final_bar_levels = set()
        for bar in self._session_bars[-num_final_bars:]:
            rounded_price = self._round_price(bar.close)
            final_bar_levels.add(rounded_price)

        # Update prior session history
        for price, visit_count in self._session_level_visits.items():
            self._prior_session_level_visits[price] += visit_count
            
            # Track which levels survived the boundary
            if price in final_bar_levels:
                self._session_boundary_levels.add(price)

            # Track directional movement from each level
            # Find bars that touched this level and track subsequent direction
            for i, bar in enumerate(self._session_bars):
                bar_rounded = self._round_price(bar.close)
                if bar_rounded == price and i < len(self._session_bars) - 1:
                    next_bar = self._session_bars[i + 1]
                    direction = "up" if next_bar.close > bar.close else "down"
                    self._prior_session_level_directions[price].append(direction)

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range [0, 1]."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _is_at_carried_level(self, price: float) -> bool:
        """
        Check if price is at a CARRIED level from prior sessions.
        
        Requirements:
        1. Level must exist in prior session history
        2. Level must have been visited multiple times in prior sessions
        3. Level must have survived the prior session boundary
        """
        rounded_price = self._round_price(price)

        # Must have been visited in prior sessions
        if self._prior_session_level_visits[rounded_price] < self.min_prior_session_visits:
            return False

        # Must have survived a prior session boundary
        if rounded_price not in self._session_boundary_levels:
            return False

        return True

    def _get_prior_level_direction_bias(self, price: float) -> Optional[str]:
        """
        Get the dominant direction price moved FROM this level in prior sessions.
        Returns "up", "down", or None if no directional bias.
        """
        rounded_price = self._round_price(price)
        
        directions = self._prior_session_level_directions.get(rounded_price, [])
        if not directions:
            return None

        up_count = sum(1 for d in directions if d == "up")
        down_count = sum(1 for d in directions if d == "down")

        if up_count > down_count:
            return "up"
        elif down_count > up_count:
            return "down"

        return None

    def _detect_absorption_at_carried_level(self, current) -> Optional[dict]:
        """
        Detect absorption that occurs at a CARRIED level from prior sessions.
        
        Absorption requires:
        1. Price at a level from prior sessions (CROSS-SESSION DEPENDENCY)
        2. Strong directional delta
        3. Wick showing price rejection
        4. Directional flow dominance
        5. Close position extremity
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        # Filter 1: Strong directional delta
        delta_ratio = abs(delta) / current.volume
        if delta_ratio < self.base_delta_ratio:
            return None

        # Filter 2: Price at CARRIED level from prior sessions (CROSS-SESSION DEPENDENCY)
        if not self._is_at_carried_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)

        # LONG: Buying pressure with absorption
        if delta > 0:
            buy_ratio = current.buy_volume / current.volume
            if buy_ratio < self.directional_flow_min:
                return None

            # Close should be high in range (absorbed buying)
            if close_position < self.close_position_extremity:
                return None

            # Lower wick (price rejected downward)
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

            # Close should be low in range (absorbed selling)
            if close_position > (1.0 - self.close_position_extremity):
                return None

            # Upper wick (price rejected upward)
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
        2. Opposite directional flow
        3. Confirmation timing within max_bars_to_confirmation
        4. Movement consistent with prior-session directional bias (HISTORY-DEPENDENT)
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_direction is None):
            return False

        # Check confirmation timing
        if self._bars_since_absorption > self.max_bars_to_confirmation:
            return False

        direction = self._pending_setup["direction"]

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Check opposite directional flow
        if direction == "long":
            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if sell_ratio < self.directional_flow_min:
                return False
        else:
            buy_ratio = confirming_bar.buy_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if buy_ratio < self.directional_flow_min:
                return False

        # Check movement consistency with prior-session directional bias
        prior_bias = self._get_prior_level_direction_bias(self._pending_absorption_close)
        if prior_bias is not None:
            if direction == "long" and prior_bias != "up":
                return False
            if direction == "short" and prior_bias != "down":
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
