"""
Volume profile absorption strategy - testing whether multi-bar confirmation improves outcomes.

The campaign explicitly requires testing whether waiting for initiation helps.
Prior trials showed that initiation logic was not triggering because thresholds
were unrealistic or placed after other filters eliminated candidates.

Analysis of best performers:
- #81: -0.0096 with 622 trades (BEST SCORE)
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

Key insight: #81's success came from strict filtering and moderate trade frequency.
The synthetic test being identical to current suggests the initiation waiting logic
never executes - the absorption bars themselves are already being filtered out.

This trial takes a different approach: instead of waiting for a separate initiation
bar, we'll test whether the absorption itself should be confirmed by looking at
what happens AFTER the absorption bar. The campaign asks: does absorption at a
carried level precede a move away from that level?

Strategy:
1. Detect absorption at carried level (using #81's parameters)
2. Instead of entering immediately, track the absorption bar
3. On the NEXT bar, check if price has moved away from the absorption level
4. Only enter if the next bar shows directional follow-through
5. Tag entries as "absorption-confirmed" so we can measure if confirmation helps

This directly tests the campaign hypothesis: absorption -> move away from level.
The confirmation happens within 1 bar, so it's fast enough to be tradeable.

Expected: Trade count will drop (more selective), but hopefully score improves
because we're trading only the absorptions that actually precede moves.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 5.0,
        min_body_ratio: float = 0.50,
        min_wick_ratio: float = 0.40,
        min_tick_count: int = 8,
        min_bar_range: float = 1.5,
        max_bar_range: float = 8.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_significance_threshold: float = 0.30,
        confirmation_move_distance: float = 1.5,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_body_ratio = min_body_ratio
        self.min_wick_ratio = min_wick_ratio
        self.min_tick_count = min_tick_count
        self.min_bar_range = min_bar_range
        self.max_bar_range = max_bar_range
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_significance_threshold = level_significance_threshold
        self.confirmation_move_distance = confirmation_move_distance

        # Level tracking with proper multi-session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
        # Track absorption bars waiting for confirmation
        self._pending_absorption: Optional[dict] = None
        self._pending_absorption_level: Optional[float] = None
        
        # Track bars we've entered on to avoid re-entry
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
            self._on_session_change(current.session)
            self._current_session = current.session

        # Accumulate volume profile for current session
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_absorption = None
            self._pending_absorption_level = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Check if pending absorption is being confirmed by this bar
        if self._pending_absorption:
            if self._check_confirmation(current):
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, self._pending_absorption)
                self._pending_absorption = None
                self._pending_absorption_level = None
                return
            else:
                # Confirmation failed - reset pending
                self._pending_absorption = None
                self._pending_absorption_level = None

        # Detect new absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            # Store as pending - wait for next bar to confirm
            self._pending_absorption = setup
            self._pending_absorption_level = current.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and carry forward."""
        if self._session_levels:
            self._prior_session_levels.append(dict(self._session_levels))
            # Keep only last 2 sessions
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_bar_timestamps = set()
        self._pending_absorption = None
        self._pending_absorption_level = None

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels with 0.25-point granularity."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract levels where volume is above significance threshold."""
        if not profile:
            return []

        max_vol = max(profile.values()) if profile else 0
        if max_vol == 0:
            return []

        threshold_vol = max_vol * self.level_significance_threshold
        selected_levels = [level for level, vol in profile.items() if vol >= threshold_vol]

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = set()
        
        # Current session levels
        all_levels.update(self._extract_significant_levels(self._session_levels))
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _check_confirmation(self, current) -> bool:
        """
        Check if current bar confirms the pending absorption.
        
        For a long absorption (buying pressure with lower wick):
        - Current bar should move away from the absorption level (higher)
        - Minimum move distance required
        
        For a short absorption (selling pressure with upper wick):
        - Current bar should move away from the absorption level (lower)
        - Minimum move distance required
        """
        if not self._pending_absorption or self._pending_absorption_level is None:
            return False

        direction = self._pending_absorption["direction"]
        absorption_level = self._pending_absorption_level

        if direction == "long":
            # For long, we want price to have moved up from absorption level
            move_distance = current.close - absorption_level
            return move_distance >= self.confirmation_move_distance
        else:
            # For short, we want price to have moved down from absorption level
            move_distance = absorption_level - current.close
            return move_distance >= self.confirmation_move_distance

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with #81's proven parameters.
        
        Absorption signature:
        - Strong directional delta (delta_ratio >= 0.28)
        - Wick in opposite direction (>= 0.40 of bar range)
        - Price near a carried level (within 5.0 points)
        - Sufficient bar characteristics (body, range, tick count)
        """
        if current.volume == 0:
            return None

        # CHECK PROXIMITY FIRST - before other filters
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # NOW apply other filters
        bar_range = current.high - current.low

        # PRE-FILTER 1: Bar range bounds
        if bar_range < self.min_bar_range or bar_range > self.max_bar_range:
            return None

        # PRE-FILTER 2: Tick count (activity level)
        if current.tick_count < self.min_tick_count:
            return None

        # PRE-FILTER 3: Body ratio (directional commitment)
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range if bar_range > 0 else 0
        if body_ratio < self.min_body_ratio:
            return None

        # Check delta and delta_ratio
        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check wick in opposite direction to delta (absorption signature)
        if delta > 0:  # Buying pressure, check lower wick
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0
            
            if wick_ratio < self.min_wick_ratio:
                return None
        
        elif delta < 0:  # Selling pressure, check upper wick
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0
            
            if wick_ratio < self.min_wick_ratio:
                return None
        
        else:
            return None

        # Calculate stop and target
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # Short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on absorption detection."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption-confirmed",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption-confirmed",
            )
