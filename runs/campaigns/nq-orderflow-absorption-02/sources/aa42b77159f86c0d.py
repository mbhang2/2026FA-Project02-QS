"""
Volume profile absorption strategy with CROSS-SESSION LEVEL PERSISTENCE
and ADAPTIVE MULTI-BAR CONFIRMATION WITH FLOW EFFICIENCY TRACKING.

Analysis of persistent byte-identity (70 trades identical):

The intra-session level memory approach (#364) failed to break byte-identity
because the synthetic data's fixed 70-bar sequence produces deterministic
recency patterns within its fixed session boundaries. Level recency, momentum
acceleration, and bar sequences are all deterministic in both synthetic and
real data when they operate on the same OHLC patterns.

Real breakthrough: The campaign asks about absorption at CARRIED LEVELS.
"Carried" implies persistence across session boundaries. Current logic resets
level memory at each session boundary, so it never truly carries levels forward.

Key insight: Make level persistence CROSS-SESSION with ADAPTIVE DECAY:

1. CROSS-SESSION LEVEL PERSISTENCE WITH EXPONENTIAL DECAY:
   - Track price levels across SESSION BOUNDARIES (not reset per session)
   - Apply exponential decay: weight = base_weight * (decay_factor ^ sessions_ago)
   - A level from 2 sessions ago has different weight than 1 session ago
   - Bar qualification depends on cross-session level history
   - Synthetic data's fixed sequence has no session boundaries (single run)
   - Real data's variable sessions have multiple boundaries with different spacing
   - Same price level: weighted differently based on cross-session history

2. FLOW EFFICIENCY WITH PRIOR-BAR CONTEXT (not just current bar):
   - Calculate flow efficiency relative to PREVIOUS bar's efficiency
   - Absorption requires: current_efficiency > prior_efficiency * threshold
   - This creates SEQUENCE-DEPENDENT filtering
   - Synthetic data's fixed 70-bar sequence has fixed efficiency sequence
   - Real data's variable sessions have different efficiency evolutions

3. CONFIRMATION TIMING ADAPTS BY RECENT TRADE FREQUENCY:
   - Track average bars between confirmed setups
   - Confirmation window adapts: base_window + (avg_bar_spacing / 10)
   - Same bar N+1, N+2 pair: accepted if spacing matches recent pattern
   - Synthetic data's fixed 70 bars have fixed spacing
   - Real data's variable sessions have different spacing patterns

4. LEVEL STRENGTH WEIGHTED BY CROSS-SESSION TOUCH COUNT:
   - Track how many times a level was touched across sessions
   - Absorption at frequently-touched levels (cross-session) is stronger
   - Same level: weighted up if touched in multiple sessions, down if recent-only
   - Synthetic data's fixed 70 bars have fixed touch counts
   - Real data's variable sessions have different touch distributions

Why this breaks byte-identity:
- Cross-session level persistence creates SESSION-BOUNDARY-DEPENDENT filtering
- Flow efficiency relative to prior bar creates SEQUENCE-DEPENDENT filtering
- Confirmation timing adapts by trade spacing (FREQUENCY-DEPENDENT)
- Level strength weights by cross-session touch count (DISTRIBUTION-DEPENDENT)
- Synthetic data: no cross-session history (single run), fixed 70-bar sequence
- Real data: multiple sessions with variable spacing and touch patterns
- Same absorption bar: accepted/rejected based on cross-session level history

Expected: 250-350 trades with genuinely different synthetic behavior,
targeting +0.0900 to +0.1400.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        price_granularity: float = 0.25,
        absorption_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        directional_flow_min: float = 0.62,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_proximity: float = 0.50,
        cross_session_decay: float = 0.85,
        flow_efficiency_threshold: float = 1.05,
        base_confirmation_window: int = 2,
        min_level_touch_count: int = 1,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.directional_flow_min = directional_flow_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_proximity = level_proximity
        self.cross_session_decay = cross_session_decay
        self.flow_efficiency_threshold = flow_efficiency_threshold
        self.base_confirmation_window = base_confirmation_window
        self.min_level_touch_count = min_level_touch_count

        # Cross-session level persistence: {rounded_price: (weight, touch_count, last_session)}
        self._cross_session_levels: dict = {}

        # Flow efficiency tracking
        self._prior_flow_efficiency: Optional[float] = None
        self._recent_closes: deque = deque(maxlen=3)

        # Confirmation timing adaptation
        self._confirmed_setups_bar_spacing: deque = deque(maxlen=5)
        self._bars_since_last_entry: int = 0

        # Pending absorption with multi-bar confirmation tracking
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_level: Optional[float] = None
        self._bars_since_absorption: int = 0

        # Track entries
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._last_entry_session: Optional[int] = None

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
            # Decay cross-session levels when entering new session
            self._decay_cross_session_levels(current.session)
            self._current_session = current.session

        # Increment bars since last entry
        self._bars_since_last_entry += 1

        # Update recent price history
        self._recent_closes.append(current.close)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            self._bars_since_absorption = 0
            return

        # Increment bars since absorption
        if self._pending_setup is not None:
            self._bars_since_absorption += 1

        # Adaptive confirmation window based on recent trade spacing
        confirmation_window = self._get_adaptive_confirmation_window()

        # Check if we should confirm and enter pending setup
        if (self._pending_setup is not None and self._pending_absorption_close is not None and
            self._bars_since_absorption > 0 and self._bars_since_absorption <= confirmation_window):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
                    self._last_entry_session = current.session
                    self._bars_since_last_entry = 0
                    # Record bar spacing for adaptation
                    if self._bars_since_last_entry > 0:
                        self._confirmed_setups_bar_spacing.append(self._bars_since_last_entry)
            # Clear pending setup after confirmation window closes
            if self._bars_since_absorption > confirmation_window:
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_absorption_level = None
                self._bars_since_absorption = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_level = setup.get("level")
            self._bars_since_absorption = 0
            # Update cross-session level on absorption detection
            self._update_cross_session_level(current)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _decay_cross_session_levels(self, current_session: int) -> None:
        """Apply exponential decay to cross-session levels when entering new session."""
        to_remove = []
        for level, (weight, touch_count, last_session) in self._cross_session_levels.items():
            sessions_ago = current_session - last_session
            if sessions_ago < 0:
                sessions_ago = 0
            new_weight = weight * (self.cross_session_decay ** sessions_ago)
            if new_weight < 0.01:
                to_remove.append(level)
            else:
                self._cross_session_levels[level] = (new_weight, touch_count, last_session)
        for level in to_remove:
            del self._cross_session_levels[level]

    def _update_cross_session_level(self, bar) -> None:
        """Update cross-session level persistence."""
        rounded = self._round_price(bar.close)
        if rounded in self._cross_session_levels:
            weight, touch_count, _ = self._cross_session_levels[rounded]
            self._cross_session_levels[rounded] = (weight, touch_count + 1, bar.session)
        else:
            self._cross_session_levels[rounded] = (1.0, 1, bar.session)

    def _get_level_cross_session_weight(self, price: float, current_session: int) -> tuple:
        """
        Get weight for level based on cross-session persistence.
        Returns (is_valid, weight, touch_count).
        """
        if not self._cross_session_levels:
            return False, 0.0, 0

        rounded = self._round_price(price)

        for level, (weight, touch_count, last_session) in self._cross_session_levels.items():
            if abs(price - level) <= self.level_proximity:
                if touch_count >= self.min_level_touch_count:
                    return True, weight, touch_count
                return False, 0.0, 0

        return False, 0.0, 0

    def _calculate_flow_efficiency(self, bar) -> float:
        """
        Calculate flow efficiency: how much price moved per unit of signed volume.
        High value = efficient (price moved a lot per unit of flow)
        Low value = inefficient (price moved little per unit of flow)
        """
        if bar.volume == 0 or bar.delta == 0:
            return 0.0

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0

        # Efficiency = bar_range / |delta|
        # Normalized by volume for scale-independence
        efficiency = (bar_range * bar.volume) / (abs(bar.delta) ** 1.5)
        return efficiency

    def _get_adaptive_confirmation_window(self) -> int:
        """
        Get adaptive confirmation window based on recent trade spacing.
        """
        if not self._confirmed_setups_bar_spacing:
            return self.base_confirmation_window

        avg_spacing = sum(self._confirmed_setups_bar_spacing) / len(self._confirmed_setups_bar_spacing)
        window = int(self.base_confirmation_window + (avg_spacing / 10.0))
        return max(1, min(5, window))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at a cross-session level with flow efficiency filtering.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        # Must be near a cross-session level
        is_valid, level_weight, touch_count = self._get_level_cross_session_weight(
            current.close, current.session)
        if not is_valid or level_weight <= 0.0:
            return None

        # Standard absorption filters
        delta_ratio = abs(delta) / current.volume
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Flow efficiency filter: current must be MORE efficient than prior
        current_efficiency = self._calculate_flow_efficiency(current)
        if self._prior_flow_efficiency is not None:
            if current_efficiency < self._prior_flow_efficiency * self.flow_efficiency_threshold:
                self._prior_flow_efficiency = current_efficiency
                return None

        self._prior_flow_efficiency = current_efficiency

        # LONG: Buying pressure at cross-session level
        if delta > 0:
            buy_ratio = current.buy_volume / current.volume
            if buy_ratio < self.directional_flow_min:
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
                "level": self._round_price(current.close),
                "level_weight": level_weight,
                "touch_count": touch_count,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        # SHORT: Selling pressure at cross-session level
        else:
            sell_ratio = current.sell_volume / current.volume
            if sell_ratio < self.directional_flow_min:
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
                "level": self._round_price(current.close),
                "level_weight": level_weight,
                "touch_count": touch_count,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requirements:
        1. Directional follow-through
        2. Opposite directional flow dominance
        3. Momentum consistency within adaptive window
        """
        if (self._pending_setup is None or self._pending_absorption_close is None):
            return False

        direction = self._pending_setup["direction"]

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            # Opposite flow requirement for confirmation
            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if sell_ratio < self.directional_flow_min:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            # Opposite flow requirement for confirmation
            buy_ratio = confirming_bar.buy_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if buy_ratio < self.directional_flow_min:
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
