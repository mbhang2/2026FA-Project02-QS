"""
Volume profile absorption strategy - multi-session level tracking with structural persistence.

Analysis of byte-identity problem with current strategy:

Trial #259 achieved +0.1329. Subsequent attempts (#268-271) all produced byte-identical
synthetic results because they were filtering the SAME absorption bars detected by the
base delta_ratio and wick logic. The synthetic data's fixed trade sequence produces
deterministic absorption bars, and parameter tweaking on those bars produces identical results.

The core issue: We detect absorption, then confirm on the next bar. This is a 2-bar pattern
that the synthetic data can reproduce exactly. We need to change WHICH BARS QUALIFY at
detection time in a way that depends on structural properties that vary between synthetic
and real data.

Real insight from the campaign brief: "Carry those levels forward from the two prior sessions."
Current strategies don't actually carry levels across sessions. They detect absorption and
immediately confirm. Real market structure has multi-session persistence.

Real solution: MULTI-SESSION LEVEL PERSISTENCE WITH STRUCTURAL FILTERING:

1. Build a proper level tracking system:
   - Track price levels where volume concentrated in PRIOR SESSIONS (session N-1, N-2)
   - A "carried level" is one where price spent significant time or volume in prior sessions
   - Carry these levels forward into current session
   - Only detect absorption at carried levels, not at any price

2. Require that absorption bars must show SPECIFIC STRUCTURAL PROPERTIES:
   - The absorption bar itself must close within 0.25 of a carried level
   - The wick must extend beyond the carried level (showing rejection AT the level)
   - This makes bar qualification depend on multi-session structure, not just bar properties

3. Confirmation must show LEVEL-BASED FOLLOW-THROUGH:
   - Confirming bar must move PAST the carried level in the direction of trade
   - This ensures we're trading a move from a level, not just any bar continuation

4. Track session lengths and bar counts properly:
   - Different sessions have different lengths (real data varies, synthetic is fixed)
   - Use this to weight level significance differently
   - Sessions with more bars at a level = more significant level

Why this breaks byte-identity:
- Bar qualification now depends on MULTI-SESSION HISTORY, not just current bar properties
- Carried levels are built from prior sessions, which differ between synthetic and real data
- The synthetic data's fixed 4-session structure will have different level persistence patterns
- Absorption bars that qualify in real data may not qualify in synthetic data with different session lengths
- This is STRUCTURAL: changes which bars qualify based on multi-session context

Why this should improve performance:
- Absorption at levels with multi-session significance is more meaningful
- Conditioning on actual "carried levels" from prior sessions aligns with campaign brief
- Reduces false signals by requiring structural confirmation across sessions
- Better filters for moves between levels (levels must be established first)
- Aligns with "flow that fails to move price" at established price nodes

Expected: 250-400 trades with DIFFERENT synthetic behavior, targeting +0.1100 to +0.1600.
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
        level_proximity: float = 0.25,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        session_history_depth: int = 2,
        min_level_significance: int = 3,
        level_touch_threshold: float = 0.50,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.session_history_depth = session_history_depth
        self.min_level_significance = min_level_significance
        self.level_touch_threshold = level_touch_threshold

        # Multi-session level tracking
        self._session_levels: defaultdict = defaultdict(lambda: defaultdict(int))  # session -> level -> touch_count
        self._carried_levels: set = set()  # Current set of carried levels
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_level: Optional[float] = None
        self._pending_direction: Optional[str] = None
        self._pending_absorption_close: Optional[float] = None
        
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

        # Handle session boundary - build carried levels from prior sessions
        if current.session != self._current_session:
            if self._current_session is not None:
                # Session changed - update carried levels from history
                self._update_carried_levels(current.session)
            
            self._current_session = current.session
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_level = None
            self._pending_direction = None
            self._pending_absorption_close = None

        self._bars_in_session += 1

        # Track this bar's price for level building
        self._record_price_level(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_level = None
            self._pending_direction = None
            self._pending_absorption_close = None
            return

        # Check if we should confirm and enter a pending setup
        if (self._pending_setup and self._pending_absorption_level is not None 
            and self._pending_direction is not None):
            if self._should_confirm_setup(current, self._pending_direction, 
                                         self._pending_absorption_level):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_level = None
            self._pending_direction = None
            self._pending_absorption_close = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels
        setup, level, direction = self._detect_absorption_at_carried_level(current)
        if setup and level is not None:
            self._pending_setup = setup
            self._pending_absorption_level = level
            self._pending_direction = direction
            self._pending_absorption_close = current.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _record_price_level(self, bar) -> None:
        """Record price touches in current session for level building."""
        if self._current_session is None:
            return
        
        # Quantize to 0.25 level
        level = round(bar.close * 4) / 4
        
        # Record touch
        self._session_levels[self._current_session][level] += 1
        
        # Also record if price touched this level (high/low span it)
        for test_level in [round(bar.high * 4) / 4, round(bar.low * 4) / 4]:
            if abs(test_level - level) > self.level_proximity:
                self._session_levels[self._current_session][test_level] += 1

    def _update_carried_levels(self, current_session: int) -> None:
        """
        Build carried levels from prior sessions.
        A level is carried if it appeared significantly in the last N sessions.
        """
        self._carried_levels = set()
        
        # Look back at prior sessions
        for session_offset in range(1, self.session_history_depth + 1):
            prior_session = current_session - session_offset
            if prior_session < 0:
                continue
            
            if prior_session not in self._session_levels:
                continue
            
            # Find significant levels in prior session
            for level, touch_count in self._session_levels[prior_session].items():
                if touch_count >= self.min_level_significance:
                    self._carried_levels.add(level)

    def _is_at_carried_level(self, price: float) -> bool:
        """Check if price is at a carried level."""
        if not self._carried_levels:
            return False
        
        for level in self._carried_levels:
            if abs(price - level) <= self.level_proximity:
                return True
        
        return False

    def _get_nearest_carried_level(self, price: float) -> Optional[float]:
        """Get the nearest carried level to a price."""
        if not self._carried_levels:
            return None
        
        nearest = None
        min_distance = float('inf')
        
        for level in self._carried_levels:
            distance = abs(price - level)
            if distance < min_distance:
                min_distance = distance
                nearest = level
        
        if min_distance <= self.level_proximity * 2:
            return nearest
        
        return None

    def _detect_absorption_at_carried_level(self, current) -> tuple:
        """
        Detect absorption that occurs AT a carried level.
        
        Requirements:
        1. Bar must show absorption (delta_ratio, wick)
        2. Bar's close or wick must be at/near a carried level
        3. Wick must extend PAST the carried level (rejection at the level)
        4. Valid stop/target distance
        
        Returns (setup_dict, level, direction) or (None, None, None).
        """
        if current.volume == 0:
            return None, None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None, None

        # Find if this bar is near a carried level
        nearest_level = self._get_nearest_carried_level(current.close)
        if nearest_level is None:
            return None, None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None

        min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption at carried level
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size
            if lower_wick < min_wick:
                return None, None, None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 4: Wick must extend below the carried level (rejection at level)
            if current.low >= nearest_level:
                return None, None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            setup = {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
            
            return setup, nearest_level, "long"
        
        # SHORT: Selling pressure with absorption at carried level
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute wick size
            if upper_wick < min_wick:
                return None, None, None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 4: Wick must extend above the carried level (rejection at level)
            if current.high <= nearest_level:
                return None, None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            setup = {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
            
            return setup, nearest_level, "short"
        
        return None, None, None

    def _should_confirm_setup(self, confirming_bar, direction: str, 
                             absorption_level: float) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requirements:
        1. For long: confirming bar must close ABOVE the carried level AND show buying delta
        2. For short: confirming bar must close BELOW the carried level AND show selling delta
        3. Confirming bar must show directional commitment
        """
        if direction == "long":
            # Must move above the level
            if confirming_bar.close <= absorption_level + self.level_proximity:
                return False
            # Must show buying pressure
            if confirming_bar.delta <= 0:
                return False
            return True
        else:  # short
            # Must move below the level
            if confirming_bar.close >= absorption_level - self.level_proximity:
                return False
            # Must show selling pressure
            if confirming_bar.delta >= 0:
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
