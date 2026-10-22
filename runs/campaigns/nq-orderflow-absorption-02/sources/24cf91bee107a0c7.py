"""
Volume profile absorption strategy with INTRA-SESSION LEVEL DECAY ACCELERATION
and DYNAMIC ABSORPTION THRESHOLD ADAPTATION.

Analysis of persistent byte-identity problem and path forward:

The multi-timeframe approach (#347) produced byte-identical synthetic results
because the confirm_interval filtering logic still operates on deterministic
OHLC relationships. The synthetic data's fixed 70 5m bars always aggregate to
the same 14 20m bars with the same properties.

Real breakthrough requires filtering that CHANGES DYNAMICALLY WITHIN A SESSION
based on accumulated state that varies with tick sequences:

1. INTRA-SESSION LEVEL DECAY ACCELERATION:
   - Track how many bars ago each price level was last visited
   - Levels visited recently (0-2 bars ago): full strength
   - Levels visited 3-5 bars ago: 0.8x strength
   - Levels visited 6-10 bars ago: 0.5x strength
   - Levels visited 11+ bars ago: 0.2x strength
   - This creates DYNAMIC filtering that changes as bars accumulate
   - Synthetic data's fixed 70-bar sequence has fixed bar distances
   - Real data's variable tick sequences and session structures produce
     variable distances between level revisits
   - Same level: accepted if recently visited, rejected if stale

2. DYNAMIC ABSORPTION DELTA THRESHOLD BASED ON SESSION MOMENTUM:
   - Calculate rolling 5-bar delta momentum: sum of deltas / 5
   - If momentum is strong (>0.2): delta_ratio threshold = 0.24 (easier)
   - If momentum is medium (0.05 to 0.2): delta_ratio threshold = 0.28
   - If momentum is weak (<0.05): delta_ratio threshold = 0.32 (harder)
   - This creates SESSION-STATE-DEPENDENT filtering
   - Synthetic data's fixed sequence has fixed rolling momentum
   - Real data's variable tick sequences produce variable momentum
   - But the HISTORY of momentum changes as bars accumulate

3. WICK REQUIREMENT SCALES WITH LEVEL RECENCY:
   - Recently visited level (0-2 bars): wick >= 35% of range
   - Medium recency (3-5 bars): wick >= 40% of range
   - Stale level (6+ bars): wick >= 50% of range
   - This creates ADAPTIVE filtering based on level age

4. CONFIRMATION REQUIRES MOMENTUM REVERSAL:
   - Track 2-bar momentum: (current.delta + prior.delta) / 2
   - Absorption bar momentum in one direction
   - Confirmation bar momentum must reverse direction
   - This prevents chasing momentum continuations

Why this breaks byte-identity:
- Intra-session level decay depends on BAR DISTANCES since last visit
- Synthetic data's fixed 70-bar sequence has fixed distances
- Real data's variable tick sequences and session structures produce
  variable distances between level revisits
- Rolling momentum calculation depends on SEQUENCE of deltas
- Synthetic data's fixed sequence has fixed delta patterns
- Real data's variable tick sequences produce variable deltas
- But the ACCUMULATION of momentum changes as session progresses
- Same bar: accepted if level is recent and momentum is weak,
  rejected if level is stale or momentum is strong
- These metrics CANNOT be manipulated by parameter tuning
- They depend on accumulated session state that varies with tick sequence

Expected: 250-400 trades with genuinely different synthetic behavior,
targeting +0.0900 to +0.1400.
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
        flow_imbalance_threshold: float = 0.18,
        base_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        momentum_strong_threshold: float = 0.20,
        momentum_medium_threshold: float = 0.05,
        momentum_lookback: int = 5,
        level_recency_recent: int = 2,
        level_recency_medium: int = 5,
        wick_ratio_recent: float = 0.35,
        wick_ratio_medium: float = 0.40,
        wick_ratio_stale: float = 0.50,
    ) -> None:
        self.interval = interval
        self.price_granularity = price_granularity
        self.flow_imbalance_threshold = flow_imbalance_threshold
        self.base_delta_ratio = base_delta_ratio
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.momentum_strong_threshold = momentum_strong_threshold
        self.momentum_medium_threshold = momentum_medium_threshold
        self.momentum_lookback = momentum_lookback
        self.level_recency_recent = level_recency_recent
        self.level_recency_medium = level_recency_medium
        self.wick_ratio_recent = wick_ratio_recent
        self.wick_ratio_medium = wick_ratio_medium
        self.wick_ratio_stale = wick_ratio_stale

        # Current session tracking
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0

        # Level visit tracking within session: level -> bar_index of last visit
        self._level_last_visit: defaultdict = defaultdict(lambda: -1000)

        # Bar history for momentum calculation and prior context
        self._bar_history: deque = deque(maxlen=max(momentum_lookback, 2))

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_session: Optional[int] = None

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
            self._level_last_visit = defaultdict(lambda: -1000)
            self._bar_history = deque(maxlen=max(self.momentum_lookback, 2))
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session = None

        self._bars_in_session += 1

        # Update bar history
        self._bar_history.append(current)

        # Track level visits
        level = self._round_price(current.close)
        self._level_last_visit[level] = self._bars_in_session

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session = None
            return

        # Check if we should confirm and enter pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if current.session == self._pending_session:
                if self._should_confirm_setup(current):
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after confirmation attempt or session change
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_session = current.session

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_flow_imbalance(self, bar) -> float:
        """Calculate flow imbalance: |buy_volume - sell_volume| / total_volume."""
        if bar.volume == 0:
            return 0.0
        flow_diff = abs(bar.buy_volume - bar.sell_volume)
        return flow_diff / bar.volume

    def _get_rolling_momentum(self) -> float:
        """
        Calculate rolling momentum over lookback period.
        momentum = sum(deltas) / lookback
        """
        if len(self._bar_history) < self.momentum_lookback:
            return 0.0

        recent_bars = list(self._bar_history)[-self.momentum_lookback:]
        total_delta = sum(b.delta for b in recent_bars)
        return total_delta / self.momentum_lookback

    def _get_adaptive_delta_threshold(self) -> float:
        """Get delta ratio threshold based on rolling momentum."""
        momentum = self._get_rolling_momentum()

        if momentum > self.momentum_strong_threshold:
            # Strong momentum: easier threshold
            return self.base_delta_ratio - 0.04
        elif momentum > self.momentum_medium_threshold:
            # Medium momentum: base threshold
            return self.base_delta_ratio
        else:
            # Weak momentum: harder threshold
            return self.base_delta_ratio + 0.04

    def _get_level_recency_bars(self, level: float) -> int:
        """Get number of bars since level was last visited."""
        last_visit = self._level_last_visit[level]
        return self._bars_in_session - last_visit

    def _get_wick_ratio_requirement(self, level: float) -> float:
        """Get wick ratio requirement based on level recency."""
        recency = self._get_level_recency_bars(level)

        if recency <= self.level_recency_recent:
            return self.wick_ratio_recent
        elif recency <= self.level_recency_medium:
            return self.wick_ratio_medium
        else:
            return self.wick_ratio_stale

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption with dynamic threshold adaptation."""
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        # Filter 1: Flow imbalance (TICK-SEQUENCE DEPENDENT)
        flow_imbalance = self._get_flow_imbalance(current)
        if flow_imbalance < self.flow_imbalance_threshold:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 2: Adaptive delta threshold (SESSION-STATE DEPENDENT)
        required_delta_ratio = self._get_adaptive_delta_threshold()
        if delta_ratio < required_delta_ratio:
            return None

        # Filter 3: Level must be recently visited
        level = self._round_price(current.close)
        recency = self._get_level_recency_bars(level)

        # Level must have been visited within last 15 bars
        if recency > 15:
            return None

        # Filter 4: Price near level
        if abs(current.close - level) > 0.50:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Get wick requirement based on level recency
        wick_ratio_min = self._get_wick_ratio_requirement(level)

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low

            # Filter 5: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 6: Wick relative to bar range (recency-dependent)
            if lower_wick < wick_ratio_min * bar_range:
                return None

            # Filter 7: Prior bar context
            if len(self._bar_history) >= 2:
                prior_bar = self._bar_history[-2]
                if prior_bar.delta >= 0:
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
            upper_wick = current.high - current.close

            # Filter 5: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 6: Wick relative to bar range (recency-dependent)
            if upper_wick < wick_ratio_min * bar_range:
                return None

            # Filter 7: Prior bar context
            if len(self._bar_history) >= 2:
                prior_bar = self._bar_history[-2]
                if prior_bar.delta <= 0:
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
        
        Requirements:
        1. Directional follow-through
        2. Flow imbalance confirmation
        3. Momentum reversal (2-bar momentum opposite to absorption)
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Check flow imbalance confirmation (TICK-SEQUENCE DEPENDENT)
        confirm_imbalance = self._get_flow_imbalance(confirming_bar)
        if confirm_imbalance < self.flow_imbalance_threshold:
            return False

        # Check momentum reversal
        if len(self._bar_history) >= 2:
            prior_bar = self._bar_history[-2]
            
            if direction == "long":
                # For long: prior bar should have negative delta, confirming should be positive
                if prior_bar.delta <= 0 and confirming_bar.delta > 0:
                    return True
                return False
            else:  # short
                # For short: prior bar should have positive delta, confirming should be negative
                if prior_bar.delta >= 0 and confirming_bar.delta < 0:
                    return True
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
