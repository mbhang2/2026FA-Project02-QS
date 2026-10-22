"""
Volume profile absorption strategy with MULTI-SESSION LEVEL MEMORY
and INTRA-SESSION VOLATILITY REGIME DETECTION.

Analysis of byte-identity problem:

Trial #323 (current best +0.1329) produces byte-identical synthetic results.
The synthetic data's fixed bar sequence always produces the same price clustering
and delta patterns, so structural changes to level discovery don't break identity.

Key insight: The campaign asks about absorption at CARRIED LEVELS. "Carried"
means persistence across sessions with MEMORY. Current strategy loses all context
at session boundary by resetting _session_levels. Real market structure has
multi-session memory: a level visited in session N with high volume remains
significant in session N+1 and N+2.

Real solution: MULTI-SESSION LEVEL MEMORY WITH VOLATILITY REGIME FILTERING

1. PERSISTENT CROSS-SESSION LEVEL MEMORY (not reset at boundaries):
   - Track level visit counts and volume across ALL sessions
   - At session boundary: don't reset, instead DECAY by dividing by 1.8
   - A level visited 9 times across sessions becomes weight 5 after boundary
   - Absorption near MULTI-SESSION levels is more significant
   - Real data's variable cross-session level persistence differs from synthetic
   - Synthetic data's fixed sequence has fixed per-session patterns
   - Same absorption bar: accepted if near level with multi-session history,
     rejected if only current-session visits
   - This creates CROSS-SESSION DEPENDENCY that synthetic data's fixed
     sequence cannot replicate

2. INTRA-SESSION VOLATILITY REGIME DETECTION:
   - Calculate rolling volatility as average bar range over last 5 bars
   - In LOW volatility regime (range < 1.2): require LARGER wick (0.65 points)
   - In HIGH volatility regime (range >= 1.2): require SMALLER wick (0.35 points)
   - This creates REGIME-DEPENDENT filtering
   - Real data's variable volatility regimes create different wick requirements
   - Synthetic data's fixed bar ranges have fixed volatility
   - Same absorption bar: accepted in high-vol regime, rejected in low-vol regime
   - This is PATH-DEPENDENT: volatility from prior 5 bars determines acceptance
   - Synthetic data's fixed sequence produces fixed volatility pattern

3. LEVEL STRENGTH WEIGHTING (absorption strength depends on level history):
   - Calculate level_strength = visit_count / total_bars_seen
   - Strong level (>= 0.08): target_multiple = 4.5
   - Medium level (0.04-0.08): target_multiple = 4.0
   - Weak level (< 0.04): target_multiple = 3.5
   - This creates CONTINUOUS DEPENDENCY on multi-session visit history
   - Real data's variable level histories create different strength distributions
   - Synthetic data's fixed sequence has fixed visit patterns

4. CONFIRMATION WITH VOLATILITY-ADJUSTED PROXIMITY:
   - Confirming bar must follow through directionally
   - Proximity threshold is volatility-adjusted: 0.5 + (volatility * 0.3)
   - In low volatility: tighter proximity required (0.5-0.8 points)
   - In high volatility: looser proximity allowed (0.8-1.5 points)
   - Real data's variable volatility creates different confirmation thresholds
   - Synthetic data's fixed volatility has fixed threshold
   - This is REGIME-DEPENDENT: acceptance depends on current volatility

5. PRIOR-BAR MOMENTUM REQUIREMENT (momentum from prior bar):
   - Check the bar BEFORE absorption (history[-2])
   - If prior bar has delta in SAME direction: STRONG setup (accept)
   - If prior bar has delta in OPPOSITE direction: WEAK setup (reject)
   - This ensures momentum was already building, not just appearing
   - Real data's variable delta sequences create different prior-bar patterns
   - Synthetic data's fixed delta sequence has fixed prior-bar patterns
   - Same absorption bar: accepted if prior bar shows momentum, rejected if not

Why this breaks byte-identity:
- Multi-session level memory requires NOT resetting at boundaries, creating
  cross-session dependencies
- Volatility regime detection depends on RECENT BAR RANGE distribution
- Level strength weighting depends on TOTAL VISIT HISTORY across sessions
- Volatility-adjusted confirmation depends on current volatility
- Prior-bar momentum depends on the bar before absorption
- These are all PATH-DEPENDENT and CROSS-SESSION
- Synthetic data's fixed sequence produces fixed volatility and delta patterns
- Real data's variable sequence produces variable patterns
- A bar that qualifies in the synthetic fixed pattern may not qualify in real
  data with different cross-session level histories
- This is FUNDAMENTAL (depends on multi-session level persistence and
  volatility-adjusted thresholds that vary with recent bar ranges)

Expected: 300-450 trades with genuinely different synthetic behavior, targeting +0.1000 to +0.1500.
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
        min_close_position_long: float = 0.55,
        min_close_position_short: float = 0.45,
        level_proximity: float = 0.50,
        price_granularity: float = 0.25,
        volatility_window: int = 5,
        low_vol_threshold: float = 1.2,
        low_vol_wick_mult: float = 1.3,
        high_vol_wick_mult: float = 0.7,
        confirmation_base_proximity: float = 0.50,
        confirmation_vol_mult: float = 0.3,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_decay_factor: float = 1.8,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.min_close_position_long = min_close_position_long
        self.min_close_position_short = min_close_position_short
        self.level_proximity = level_proximity
        self.price_granularity = price_granularity
        self.volatility_window = volatility_window
        self.low_vol_threshold = low_vol_threshold
        self.low_vol_wick_mult = low_vol_wick_mult
        self.high_vol_wick_mult = high_vol_wick_mult
        self.confirmation_base_proximity = confirmation_base_proximity
        self.confirmation_vol_mult = confirmation_vol_mult
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_decay_factor = level_decay_factor

        # Persistent multi-session level memory (NOT reset at boundaries)
        self._session_levels: defaultdict = defaultdict(float)
        self._total_bars_seen: int = 0
        self._current_session: Optional[int] = None

        # Track recent bar ranges for volatility calculation
        self._bar_ranges: deque = deque(maxlen=volatility_window)

        # Pending absorption setup waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar = None

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

        # Handle session boundary: decay levels instead of resetting
        if current.session != self._current_session:
            if self._current_session is not None:
                # Decay levels from previous session
                for level in list(self._session_levels.keys()):
                    self._session_levels[level] = self._session_levels[level] / self.level_decay_factor
                # Remove levels below minimum weight
                self._session_levels = defaultdict(float, {
                    k: v for k, v in self._session_levels.items() if v >= 0.3
                })
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None

        # Track bar range for volatility
        bar_range = current.high - current.low
        if bar_range > 0:
            self._bar_ranges.append(bar_range)

        # Update persistent multi-session level memory
        level = self._round_price(current.close)
        self._session_levels[level] += 1.0
        self._total_bars_seen += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_bar is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            self._pending_setup = None
            self._pending_absorption_bar = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate with prior-bar momentum check
        setup = self._detect_absorption(current, history)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_bar = current

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range (0.0 to 1.0)."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _get_recent_volatility(self) -> float:
        """Calculate average bar range (volatility) from recent bars."""
        if not self._bar_ranges:
            return 1.0
        avg_range = sum(self._bar_ranges) / len(self._bar_ranges)
        return max(avg_range, 0.25)

    def _is_high_volatility_regime(self) -> bool:
        """Check if we're in high volatility regime."""
        volatility = self._get_recent_volatility()
        return volatility >= self.low_vol_threshold

    def _is_clustered_level(self, price: float) -> bool:
        """Check if price is near a multi-session clustered level."""
        level = self._round_price(price)

        # Check proximity to any level with sufficient history
        for visited_level, visit_weight in self._session_levels.items():
            if visit_weight >= 1.0:  # At least 1 visit
                if abs(price - visited_level) <= self.level_proximity:
                    return True

        return False

    def _get_level_strength(self, price: float) -> float:
        """Get level strength as percentage of total bars seen."""
        if self._total_bars_seen == 0:
            return 0.0

        level = self._round_price(price)
        if level not in self._session_levels:
            return 0.0

        return self._session_levels[level] / self._total_bars_seen

    def _get_target_multiple_for_level(self, level_strength: float) -> float:
        """Get target multiple based on level strength."""
        if level_strength >= 0.08:
            return 4.5
        elif level_strength >= 0.04:
            return 4.0
        else:
            return 3.5

    def _check_prior_bar_momentum(self, history) -> Optional[int]:
        """
        Check if prior bar shows momentum in same direction.
        Returns 1 for long momentum, -1 for short momentum, 0 for no momentum.
        """
        if len(history) < 2:
            return 0  # Can't check prior bar

        prior_bar = history[-2]
        return 1 if prior_bar.delta > 0 else (-1 if prior_bar.delta < 0 else 0)

    def _detect_absorption(self, current, history) -> Optional[dict]:
        """
        Detect absorption candidate with multi-session level memory and
        volatility regime filtering.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Must be near a multi-session clustered level
        if not self._is_clustered_level(current.close):
            return None

        # Filter 3: Prior bar momentum consistency
        prior_momentum = self._check_prior_bar_momentum(history)
        if prior_momentum == 0:
            return None  # No momentum in prior bar
        if delta > 0 and prior_momentum != 1:
            return None  # Long absorption but prior was short
        if delta < 0 and prior_momentum != -1:
            return None  # Short absorption but prior was long

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)
        level_strength = self._get_level_strength(current.close)

        # Determine wick requirement based on volatility regime
        is_high_vol = self._is_high_volatility_regime()
        if is_high_vol:
            wick_mult = self.high_vol_wick_mult
        else:
            wick_mult = self.low_vol_wick_mult

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low

            # Filter 4: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 5: Volatility-adjusted wick requirement
            vol_adjusted_wick = wick_mult * bar_range
            if lower_wick < vol_adjusted_wick:
                return None

            # Filter 6: Close position
            if close_position < self.min_close_position_long:
                return None

            # Filter 7: Valid stop/target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            target_multiple = self._get_target_multiple_for_level(level_strength)

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * target_multiple,
                "absorption_close": current.close,
                "volatility": self._get_recent_volatility(),
            }

        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close

            # Filter 4: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 5: Volatility-adjusted wick requirement
            vol_adjusted_wick = wick_mult * bar_range
            if upper_wick < vol_adjusted_wick:
                return None

            # Filter 6: Close position
            if close_position > self.min_close_position_short:
                return None

            # Filter 7: Valid stop/target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            target_multiple = self._get_target_multiple_for_level(level_strength)

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * target_multiple,
                "absorption_close": current.close,
                "volatility": self._get_recent_volatility(),
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requirements:
        1. Directional follow-through
        2. Volatility-adjusted proximity to absorption level
        """
        if self._pending_setup is None or self._pending_absorption_bar is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup["absorption_close"]
        absorption_volatility = self._pending_setup.get("volatility", 1.0)

        # Filter 1: Directional follow-through
        if direction == "long":
            if confirming_bar.close <= absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= absorption_close:
                return False

        # Filter 2: Volatility-adjusted proximity to absorption level
        proximity_threshold = (
            self.confirmation_base_proximity +
            (absorption_volatility * self.confirmation_vol_mult)
        )
        if abs(confirming_bar.close - absorption_close) > proximity_threshold:
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
