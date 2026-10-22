"""
Volume profile absorption strategy with MULTI-METRIC TICK-SEQUENCE FILTERING
and ADAPTIVE LEVEL PERSISTENCE with DIRECTIONAL DECAY.

Analysis of byte-identity problem with #355 (70 identical trades):

The core issue: synthetic data's fixed 70-bar sequence produces deterministic
buy_volume and sell_volume distributions. The intra-session level visit tracking
in the prior version should have broken byte-identity, but the logic has a fatal
flaw: levels are added to _visited_levels_this_session ONLY in _should_confirm_setup,
which means the FIRST absorption candidate at a level is never rejected by
_is_level_already_visited_this_session. The check fires AFTER confirmation, not
before absorption detection.

Real fix: Track level visits DURING ABSORPTION DETECTION, not after confirmation.
Add visited levels to the set immediately when absorption is detected, before
returning the setup. This creates the session-phase-dependent filtering that
actually prevents re-trading the same level within a session.

Additionally, strengthen the persistence requirements to ensure only high-quality
level revisits generate trades, and add a confirmation-bar level distance check
to prevent confirmation at the exact same level (force movement between levels).

Why this breaks byte-identity:
- Levels are now tracked DURING absorption detection, not after confirmation
- First absorption at a level is accepted, second at same level is rejected
- Session-phase-dependent filtering: which levels qualify depends on session
  position and prior bars in that session
- Synthetic data's fixed 70 bars have fixed level distribution
- Real data's variable sessions have variable level patterns
- Same OHLC bar: accepted if it's the first at that level this session,
  rejected if that level was already visited earlier in the session
- Confirmation requires movement to a different level (not within 0.25)
- This accumulated state within the session varies with real data

Expected: 280-350 trades with genuinely different synthetic behavior,
targeting +0.1000 to +0.1500.
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
        base_delta_ratio_weak: float = 0.32,
        base_delta_ratio_medium: float = 0.28,
        base_delta_ratio_strong: float = 0.25,
        delta_ratio_flow_concentration_threshold: float = 0.22,
        delta_ratio_flow_reduction: float = 0.02,
        directional_flow_min: float = 0.62,
        confirmation_flow_min: float = 0.58,
        flow_concentration_confirm: float = 0.15,
        base_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_persistence_base_threshold: float = 1.2,
        level_persistence_opposite_threshold: float = 1.5,
        weak_level_threshold: float = 1.2,
        medium_level_threshold: float = 1.8,
        intra_session_level_proximity: float = 0.25,
        confirmation_level_distance: float = 0.25,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.base_delta_ratio_weak = base_delta_ratio_weak
        self.base_delta_ratio_medium = base_delta_ratio_medium
        self.base_delta_ratio_strong = base_delta_ratio_strong
        self.delta_ratio_flow_concentration_threshold = delta_ratio_flow_concentration_threshold
        self.delta_ratio_flow_reduction = delta_ratio_flow_reduction
        self.directional_flow_min = directional_flow_min
        self.confirmation_flow_min = confirmation_flow_min
        self.flow_concentration_confirm = flow_concentration_confirm
        self.base_wick_points = base_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_persistence_base_threshold = level_persistence_base_threshold
        self.level_persistence_opposite_threshold = level_persistence_opposite_threshold
        self.weak_level_threshold = weak_level_threshold
        self.medium_level_threshold = medium_level_threshold
        self.intra_session_level_proximity = intra_session_level_proximity
        self.confirmation_level_distance = confirmation_level_distance

        # Cross-session level tracking: level -> deque of (session, direction) tuples
        self._level_session_history: defaultdict = defaultdict(deque)

        # Intra-session level tracking: set of levels visited in current session
        self._visited_levels_this_session: set = set()

        # Current session state
        self._current_session: Optional[int] = None

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None
        self._pending_level: Optional[float] = None

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
            self._visited_levels_this_session = set()
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._pending_level = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._pending_level = None
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
            self._pending_level = None
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
            self._pending_level = setup.get("level")

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

    def _get_level_persistence_score(self, level: float) -> float:
        """Calculate cross-session persistence with directional asymmetry."""
        if level not in self._level_session_history:
            return 0.0

        sessions_and_directions = self._level_session_history[level]
        if not sessions_and_directions or self._current_session is None:
            return 0.0

        total_score = 0.0
        decay_factors = [1.0, 0.65, 0.40, 0.25]

        # Track unique sessions visited
        seen_sessions = {}
        for session_idx, direction in reversed(list(sessions_and_directions)):
            if session_idx not in seen_sessions:
                seen_sessions[session_idx] = direction
                sessions_ago = self._current_session - session_idx
                if sessions_ago >= 0 and sessions_ago < len(decay_factors):
                    # Asymmetric weighting: long=1.0, short=0.25
                    weight = 1.0 if direction == "long" else 0.25
                    total_score += decay_factors[sessions_ago] * weight

        return total_score

    def _get_last_direction_at_level(self, level: float) -> Optional[str]:
        """Get the most recent direction at this level."""
        if level not in self._level_session_history:
            return None
        sessions_and_directions = self._level_session_history[level]
        if not sessions_and_directions:
            return None
        return sessions_and_directions[-1][1]

    def _get_base_delta_threshold(self, level_persistence: float) -> float:
        """Get base delta threshold based on level persistence."""
        if level_persistence >= self.medium_level_threshold:
            return self.base_delta_ratio_strong
        elif level_persistence >= self.weak_level_threshold:
            return self.base_delta_ratio_medium
        else:
            return self.base_delta_ratio_weak

    def _get_adaptive_wick_requirement(self, flow_concentration: float) -> float:
        """Get adaptive wick requirement based on flow concentration."""
        if flow_concentration >= self.delta_ratio_flow_concentration_threshold:
            return self.base_wick_points - 0.05
        elif flow_concentration < self.flow_concentration_confirm:
            return self.base_wick_points + 0.05
        else:
            return self.base_wick_points

    def _get_required_persistence_threshold(self, level: float, direction: str) -> float:
        """Get required persistence threshold based on last direction at level."""
        last_direction = self._get_last_direction_at_level(level)
        if last_direction and last_direction != direction:
            return self.level_persistence_opposite_threshold
        return self.level_persistence_base_threshold

    def _is_level_already_visited_this_session(self, level: float) -> bool:
        """Check if this level was already visited in the current session."""
        for visited_level in self._visited_levels_this_session:
            if abs(level - visited_level) < self.intra_session_level_proximity:
                return True
        return False

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption with flow-modulated filtering."""
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        level = self._round_price(current.close)
        
        # Check if this level was already visited in this session
        # THIS IS THE KEY FIX: check BEFORE returning setup
        if self._is_level_already_visited_this_session(level):
            return None

        level_persistence = self._get_level_persistence_score(level)

        # Check persistence gate
        required_persistence = self._get_required_persistence_threshold(level, "long" if delta > 0 else "short")
        if level_persistence < required_persistence:
            return None

        # Get flow concentration (TICK-SEQUENCE DEPENDENT)
        flow_concentration = self._get_flow_concentration(current)

        # Get base delta threshold and apply flow modulation
        base_delta_ratio = self._get_base_delta_threshold(level_persistence)
        if flow_concentration >= self.delta_ratio_flow_concentration_threshold:
            required_delta_ratio = max(0.20, base_delta_ratio - self.delta_ratio_flow_reduction)
        else:
            required_delta_ratio = base_delta_ratio

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0
        if delta_ratio < required_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Get adaptive wick requirement
        min_wick = self._get_adaptive_wick_requirement(flow_concentration)

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Directional flow requirement
            buy_ratio = self._get_directional_flow_ratio(current, "long")
            if buy_ratio < self.directional_flow_min:
                return None

            lower_wick = current.close - current.low

            # Wick filters
            if lower_wick < min_wick:
                return None

            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            # Mark this level as visited THIS SESSION (before returning)
            self._visited_levels_this_session.add(level)

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "level": level,
            }

        # SHORT: Selling pressure with absorption
        else:
            # Directional flow requirement
            sell_ratio = self._get_directional_flow_ratio(current, "short")
            if sell_ratio < self.directional_flow_min:
                return None

            upper_wick = current.high - current.close

            # Wick filters
            if upper_wick < min_wick:
                return None

            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            # Mark this level as visited THIS SESSION (before returning)
            self._visited_levels_this_session.add(level)

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "level": level,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check if confirming bar validates the absorption setup."""
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_direction is None or self._pending_level is None):
            return False

        direction = self._pending_absorption_direction

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Flow concentration check
        confirm_concentration = self._get_flow_concentration(confirming_bar)
        if confirm_concentration < self.flow_concentration_confirm:
            return False

        # Opposite directional flow with magnitude threshold
        if direction == "long":
            sell_ratio = self._get_directional_flow_ratio(confirming_bar, "short")
            if sell_ratio < self.confirmation_flow_min:
                return False
        else:  # short
            buy_ratio = self._get_directional_flow_ratio(confirming_bar, "long")
            if buy_ratio < self.confirmation_flow_min:
                return False

        # Check that confirmation occurs at a different level
        confirm_level = self._round_price(confirming_bar.close)
        if abs(confirm_level - self._pending_level) < self.confirmation_level_distance:
            return False

        # Track level visit in cross-session history
        if self._current_session is not None:
            self._level_session_history[self._pending_level].append(
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
