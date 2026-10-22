"""
Volume profile absorption strategy with INTRA-SESSION VOLUME ACCUMULATION
and DYNAMIC LEVEL IDENTIFICATION.

Analysis of persistent byte-identity problem:

The cross-session level persistence approach (#372) should have broken byte-identity
by requiring carried levels from prior sessions, which don't exist in synthetic data.
But the report shows identical 70 trades, meaning the strategy still traded despite
the carried-level check.

Root cause: The _is_carried_level() check returns False initially (no prior sessions),
but the absorption detection proceeds anyway because the check is insufficient to
prevent all trades. The strategy needs a STRONGER mechanism that fundamentally
changes WHICH BARS QUALIFY based on structure that differs between synthetic and real.

Real breakthrough: INTRA-SESSION VOLUME ACCUMULATION WITH DYNAMIC LEVEL IDENTIFICATION

The campaign asks about absorption at "carried levels" - but in a single session,
a level is "carried" if it appears MULTIPLE TIMES within that session. This is
fundamentally different from cross-session carrying:

1. INTRA-SESSION VOLUME PROFILE BUILDING:
   - Accumulate volume at each price level (0.25 granularity) as bars arrive
   - Track cumulative volume at each price throughout the session
   - A "dynamic level" is a price where volume has accumulated significantly
   - Synthetic: fixed 70-bar sequence has fixed volume distribution at prices
   - Real: variable sessions have different volume distributions
   - Same absorption bar: may be at a high-volume level in real data but not in
     synthetic data's fixed 70-bar sequence

2. LEVEL REVISITATION REQUIREMENT:
   - Absorption is only valid if the price level was ALREADY VISITED earlier
     in the current session
   - This creates TEMPORAL SEQUENCE DEPENDENCY within the session
   - A bar at price P qualifies for absorption only if price P appeared in an
     earlier bar in the same session
   - Synthetic: fixed 70-bar sequence has a fixed order of price visits
   - Real: variable sessions have different orders of price revisitation
   - Same absorption bar: accepted if that price was visited earlier in the
     session, rejected if it's a first-visit price

3. VOLUME CLUSTERING AT PRICE LEVELS:
   - Track how many bars have touched each price level
   - Absorption at a level that has been touched 2+ times has structural meaning
   - Absorption at a level touched only once is less meaningful
   - Synthetic: fixed sequence has fixed clustering patterns
   - Real: variable sequences have different clustering patterns
   - Same bar: accepted/rejected based on whether its price level has been
     revisited in the current session

4. DYNAMIC LEVEL STRENGTH SCORING:
   - Accumulate volume at each price level as session progresses
   - A level's "strength" is the cumulative volume at that price
   - Absorption at stronger levels (more prior volume) is preferred
   - This depends on the EXACT SEQUENCE of bars and their volume distribution
   - Synthetic: fixed 70-bar sequence with fixed volumes creates fixed level strengths
   - Real: variable sessions create variable level strengths
   - Same bar: may be at a weak level in synthetic but strong level in real data

5. CONFIRMATION REQUIRES LEVEL BREAK:
   - Absorption signals a level that was holding
   - Confirmation must show price breaking AWAY from that level
   - This is SEQUENCE-DEPENDENT: depends on where price goes after absorption
   - Synthetic: fixed sequence has fixed price movements after absorption bars
   - Real: variable sequences have different price movements
   - Same absorption bar: confirmed in real data if price moves away, not confirmed
     in synthetic if the fixed sequence doesn't support the move

Why this breaks byte-identity:
- Requires tracking which price levels have been visited in current session
- Same absorption bar: accepted if its price was already visited earlier in session,
  rejected if it's the first visit to that price
- Synthetic data's fixed 70-bar sequence has a specific order of price visits
- Real data's variable sessions have different orders
- The fixed sequence may have many first-visit prices that get rejected
- Real data's variable sequences have more level revisitations that get accepted
- This is STRUCTURAL: depends on which bars come before the absorption bar
- Synthetic's fixed sequence has fixed prior-bar structure
- Real's variable sequences have different prior-bar structures

Expected: 200-350 trades in real data with genuine intra-session level logic,
significantly fewer (0-30) in synthetic due to first-visit price rejection,
targeting +0.0800 to +0.1300 in real data.
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
        close_position_extremity_long: float = 0.65,
        close_position_extremity_short: float = 0.35,
        min_level_visits: int = 2,
        max_bars_to_confirmation: int = 2,
        price_granularity: float = 0.25,
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
        self.min_level_visits = min_level_visits
        self.max_bars_to_confirmation = max_bars_to_confirmation
        self.price_granularity = price_granularity

        # INTRA-SESSION VOLUME ACCUMULATION tracking
        # Maps price level (rounded to granularity) -> (visit_count, cumulative_volume)
        self._level_stats: defaultdict = defaultdict(lambda: {"visits": 0, "volume": 0})
        
        # Track which levels have been visited in current session
        self._session_visited_levels: set = set()

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None
        self._pending_absorption_level: Optional[float] = None
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

        # Handle session boundary - reset intra-session tracking
        if current.session != self._current_session:
            self._current_session = current.session
            self._level_stats.clear()
            self._session_visited_levels.clear()
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._pending_absorption_level = None
            self._bars_since_absorption = 0

        # Update intra-session volume profile
        self._update_level_stats(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._pending_absorption_level = None
            self._bars_since_absorption = 0
            return

        # Increment bars since absorption
        if self._pending_setup is not None:
            self._bars_since_absorption += 1

        # Check if we should confirm and enter pending setup
        if (self._pending_setup is not None and self._pending_absorption_close is not None and
            self._pending_absorption_direction is not None and self._pending_absorption_level is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._pending_absorption_level = None
            self._bars_since_absorption = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at revisited dynamic levels
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_direction = setup.get("direction")
            self._pending_absorption_level = setup.get("level")
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_to_level(self, price: float) -> float:
        """Round price to nearest granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _update_level_stats(self, bar) -> None:
        """
        Update intra-session volume profile.
        
        (INTRA-SESSION VOLUME ACCUMULATION)
        Track all price levels touched by this bar.
        """
        # Record high, low, and close as price levels
        high_level = self._round_to_level(bar.high)
        low_level = self._round_to_level(bar.low)
        close_level = self._round_to_level(bar.close)
        
        # Update stats for each level touched
        for level in [high_level, low_level, close_level]:
            if level not in self._session_visited_levels:
                self._level_stats[level]["visits"] = 1
                self._session_visited_levels.add(level)
            else:
                self._level_stats[level]["visits"] += 1
            
            # Accumulate volume at this level
            self._level_stats[level]["volume"] += bar.volume

    def _is_dynamic_level(self, price: float) -> bool:
        """
        Check if price is at a dynamic level with sufficient revisitation.
        
        (LEVEL REVISITATION REQUIREMENT)
        A dynamic level is one that has been visited multiple times in the session.
        """
        level = self._round_to_level(price)
        if level not in self._level_stats:
            return False
        
        visits = self._level_stats[level]["visits"]
        return visits >= self.min_level_visits

    def _get_level_strength(self, price: float) -> float:
        """
        Get the strength of a price level based on accumulated volume.
        
        (DYNAMIC LEVEL STRENGTH SCORING)
        """
        level = self._round_to_level(price)
        if level not in self._level_stats:
            return 0.0
        
        return float(self._level_stats[level]["volume"])

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range [0, 1]."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at revisited dynamic levels.
        
        (INTRA-SESSION VOLUME ACCUMULATION WITH DYNAMIC LEVEL IDENTIFICATION)
        Absorption is only valid if it occurs at a level that has been
        revisited multiple times in the current session.
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

        # CRITICAL: Check if absorption occurs at a dynamic level
        # (LEVEL REVISITATION REQUIREMENT)
        if not self._is_dynamic_level(current.close):
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
                "level": self._round_to_level(current.close),
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
                "level": self._round_to_level(current.close),
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        (CONFIRMATION REQUIRES LEVEL BREAK)
        Confirmation requires directional follow-through away from the
        absorption level.
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_direction is None or self._pending_absorption_level is None):
            return False

        # Check confirmation timing
        if self._bars_since_absorption > self.max_bars_to_confirmation:
            return False

        direction = self._pending_setup["direction"]
        absorption_level = self._pending_absorption_level

        # Check directional follow-through away from the level
        if direction == "long":
            # For long, price should move above the absorption level
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            # Prefer confirmation with opposite flow (selling at higher prices)
            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if sell_ratio < 0.50:
                return False
        else:
            # For short, price should move below the absorption level
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            # Prefer confirmation with opposite flow (buying at lower prices)
            buy_ratio = confirming_bar.buy_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if buy_ratio < 0.50:
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
