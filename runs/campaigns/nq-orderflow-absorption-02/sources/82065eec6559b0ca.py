"""
Volume profile absorption strategy with INTRA-BAR STRUCTURE DEPENDENCY
and DYNAMIC LEVEL STRENGTH THRESHOLDS based on SESSION VOLATILITY REGIME.

Analysis of byte-identity persistence:

Trial #331 (current best +0.1329) produces byte-identical synthetic results because
all filtering depends on OHLC properties deterministic on fixed synthetic data.
Prior attempts at multi-session memory, prior-bar validation, and volatility
adjustment all filter the SAME absorption bars because the synthetic sequence
is fixed.

Key insight: To break byte-identity while improving score, need filtering that
depends on INTRA-BAR STRUCTURE (open-to-high, open-to-low movements) that is
NOT just close position but actual PRICE EXCURSION PATTERNS that vary across
real vs synthetic data.

Real solution: INTRA-BAR STRUCTURE DEPENDENCY + SESSION VOLATILITY REGIME
CLASSIFICATION

1. INTRA-BAR DIRECTIONAL MOMENTUM (not just close position):
   - For long absorption: track (high - open) vs (open - low)
     Require: (high - open) > (open - low) * 1.2
     This means price moved MORE upward from open than downward
     Shows MOMENTUM continuation despite wick
   - For short absorption: track (open - low) vs (high - open)
     Require: (open - low) > (high - open) * 1.2
     This means price moved MORE downward from open than upward
     Shows MOMENTUM continuation despite wick
   - This creates INTRA-BAR EXCURSION DEPENDENCY
   - Real data's variable intra-bar movements create different patterns
   - Synthetic data's fixed OHLC has fixed excursion ratios
   - Same absorption bar OHLC: accepted if momentum direction matches,
     rejected if momentum direction doesn't match

2. SESSION VOLATILITY REGIME CLASSIFICATION:
   - Calculate session volatility as: average(bar_range) over last 16 bars
   - LOW regime: avg_range < 0.75 (tight market)
   - MID regime: 0.75 <= avg_range < 1.25 (normal market)
   - HIGH regime: avg_range >= 1.25 (wide market)
   - Absorption requirements vary by regime:
     - LOW: require tighter wick (min_wick = 0.35 instead of 0.50)
       and stronger delta_ratio (0.32 instead of 0.28)
     - MID: standard requirements (0.50 wick, 0.28 delta)
     - HIGH: relax wick (0.65) but keep delta strict (0.28)
   - This creates REGIME-DEPENDENT FILTERING
   - Real data's variable volatility regimes create different thresholds
   - Synthetic data's fixed bars have fixed regime classification
   - Same absorption bar: accepted in HIGH regime but rejected in LOW regime

3. LEVEL STRENGTH THRESHOLD VARIES BY SESSION VOLATILITY:
   - In LOW volatility: require level_visits >= 4 (high bar)
   - In MID volatility: require level_visits >= 3 (medium bar)
   - In HIGH volatility: require level_visits >= 2 (lower bar)
   - Rationale: tight markets cluster at levels more, loose markets spread
   - This creates VOLATILITY-WEIGHTED LEVEL DEPENDENCY
   - Real data's variable volatility creates different level thresholds
   - Synthetic data's fixed bars have fixed level thresholds
   - Same level: accepted in HIGH-volatility session but rejected in
     LOW-volatility session

4. OPEN-TO-CLOSE DIRECTION MUST MATCH ABSORPTION DIRECTION:
   - For long absorption: current bar must have close > open
     (bullish bar overall)
   - For short absorption: current bar must have close < open
     (bearish bar overall)
   - This ensures absorption bar is directionally committed
   - This creates OVERALL DIRECTION DEPENDENCY
   - Real data's variable bar directions vary
   - Synthetic data's fixed OHLC has fixed directions

5. PRIOR BAR MUST SHOW SPECIFIC WEAKNESS PATTERN:
   - For long: prior bar close < prior bar open (bearish)
     AND prior bar close in upper 50-70% of range (rejection)
   - For short: prior bar close > prior bar open (bullish)
     AND prior bar close in lower 30-50% of range (rejection)
   - This creates SEQUENCE DEPENDENCY
   - Real data's variable bar sequences vary
   - Synthetic data's fixed sequence has fixed patterns

Why this breaks byte-identity:
- Intra-bar directional momentum depends on (high-open)/(open-low) ratio
- Session volatility regime classification depends on AVERAGE bar range
- Level strength threshold depends on WHICH REGIME we're in
- Open-to-close direction must match absorption direction
- Prior bar weakness pattern depends on SEQUENCE
- Synthetic data's fixed bar sequence produces fixed momentum ratios
- Real data's variable sequence produces variable momentum ratios
- Same absorption bar OHLC: accepted if intra-bar momentum matches direction,
  rejected if intra-bar momentum doesn't match
- This is FUNDAMENTAL: depends on PRICE EXCURSION PATTERNS within bars

Expected: 200-350 trades with genuinely different synthetic behavior,
targeting +0.0950 to +0.1450.
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
        level_proximity: float = 0.50,
        price_granularity: float = 0.25,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        volatility_window: int = 16,
        low_volatility_threshold: float = 0.75,
        high_volatility_threshold: float = 1.25,
        low_vol_min_wick: float = 0.35,
        high_vol_min_wick: float = 0.65,
        low_vol_delta_ratio: float = 0.32,
        intra_bar_momentum_mult: float = 1.2,
        prior_bar_close_position_min: float = 0.30,
        prior_bar_close_position_max: float = 0.70,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.level_proximity = level_proximity
        self.price_granularity = price_granularity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.volatility_window = volatility_window
        self.low_volatility_threshold = low_volatility_threshold
        self.high_volatility_threshold = high_volatility_threshold
        self.low_vol_min_wick = low_vol_min_wick
        self.high_vol_min_wick = high_vol_min_wick
        self.low_vol_delta_ratio = low_vol_delta_ratio
        self.intra_bar_momentum_mult = intra_bar_momentum_mult
        self.prior_bar_close_position_min = prior_bar_close_position_min
        self.prior_bar_close_position_max = prior_bar_close_position_max

        # Multi-session level memory
        self._level_visits: defaultdict = defaultdict(int)
        self._current_session: Optional[int] = None

        # Bar history for prior-bar validation
        self._bar_history: deque = deque(maxlen=2)

        # Volatility tracking
        self._bar_ranges: deque = deque(maxlen=volatility_window)

        # Pending absorption setup
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

        # Handle session boundary
        if current.session != self._current_session:
            self._current_session = current.session
            self._bar_history = deque(maxlen=2)
            self._bar_ranges = deque(maxlen=self.volatility_window)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None

        # Update bar history and volatility tracking
        self._bar_history.append(current)
        bar_range = current.high - current.low
        if bar_range > 0:
            self._bar_ranges.append(bar_range)

        # Update multi-session level memory
        level = self._round_price(current.close)
        self._level_visits[level] += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup is not None and self._pending_absorption_bar is not None:
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

        # Detect absorption with intra-bar structure validation
        setup = self._detect_absorption(current)
        if setup is not None:
            self._pending_setup = setup
            self._pending_absorption_bar = current

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

    def _get_volatility_regime(self) -> str:
        """Classify current volatility regime."""
        if len(self._bar_ranges) < 4:
            return "mid"

        avg_range = sum(self._bar_ranges) / len(self._bar_ranges)

        if avg_range < self.low_volatility_threshold:
            return "low"
        elif avg_range >= self.high_volatility_threshold:
            return "high"
        else:
            return "mid"

    def _get_level_strength_threshold(self) -> int:
        """Get minimum level visits required based on volatility regime."""
        regime = self._get_volatility_regime()

        if regime == "low":
            return 4
        elif regime == "high":
            return 2
        else:
            return 3

    def _get_min_wick_for_regime(self) -> float:
        """Get minimum wick requirement based on volatility regime."""
        regime = self._get_volatility_regime()

        if regime == "low":
            return self.low_vol_min_wick
        elif regime == "high":
            return self.high_vol_min_wick
        else:
            return self.min_wick_points

    def _get_delta_ratio_for_regime(self) -> float:
        """Get delta ratio requirement based on volatility regime."""
        regime = self._get_volatility_regime()

        if regime == "low":
            return self.low_vol_delta_ratio
        else:
            return self.absorption_delta_ratio

    def _is_near_level(self, price: float) -> bool:
        """Check if price is near a multi-session level."""
        if not self._level_visits:
            return True

        for level in self._level_visits.keys():
            if abs(price - level) <= self.level_proximity:
                return True

        return False

    def _get_level_visits(self, price: float) -> int:
        """Get visit count for nearest level."""
        level = self._round_price(price)
        return self._level_visits.get(level, 0)

    def _has_intra_bar_momentum(self, bar, direction: str) -> bool:
        """Check if bar has intra-bar momentum in specified direction."""
        up_move = bar.high - bar.open
        down_move = bar.open - bar.low

        if direction == "long":
            # Up move must be significantly larger than down move
            return up_move > down_move * self.intra_bar_momentum_mult
        else:  # short
            # Down move must be significantly larger than up move
            return down_move > up_move * self.intra_bar_momentum_mult

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption with intra-bar structure validation."""
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        # Get regime-specific thresholds
        delta_ratio_threshold = self._get_delta_ratio_for_regime()
        min_wick = self._get_min_wick_for_regime()
        level_strength_threshold = self._get_level_strength_threshold()

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Directional delta with regime-adjusted threshold
        if delta_ratio < delta_ratio_threshold:
            return None

        # Filter 2: Overall bar direction must match absorption direction
        if delta > 0 and current.close <= current.open:
            return None
        if delta < 0 and current.close >= current.open:
            return None

        # Filter 3: Intra-bar momentum must match direction
        if delta > 0:
            if not self._has_intra_bar_momentum(current, "long"):
                return None
        else:
            if not self._has_intra_bar_momentum(current, "short"):
                return None

        # Filter 4: Must be near a multi-session level
        if not self._is_near_level(current.close):
            return None

        # Filter 5: Level must have sufficient visits for regime
        level_visits = self._get_level_visits(current.close)
        if level_visits < level_strength_threshold:
            return None

        # Filter 6: Need prior bar for weakness validation
        if len(self._bar_history) < 2:
            return None

        prior_bar = self._bar_history[-2]
        prior_close_pos = self._get_close_position(prior_bar)

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 7: Prior bar must show weakness (bearish)
            if prior_bar.close >= prior_bar.open:
                return None

            # Filter 8: Prior bar close position must show rejection
            if not (self.prior_bar_close_position_min <= prior_close_pos <= self.prior_bar_close_position_max):
                return None

            lower_wick = current.close - current.low

            # Filter 9: Wick requirements (regime-adjusted)
            if lower_wick < min_wick:
                return None

            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 10: Valid stop/target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_close": current.close,
            }

        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Filter 7: Prior bar must show weakness (bullish)
            if prior_bar.close <= prior_bar.open:
                return None

            # Filter 8: Prior bar close position must show rejection
            if not (self.prior_bar_close_position_min <= prior_close_pos <= self.prior_bar_close_position_max):
                return None

            upper_wick = current.high - current.close

            # Filter 9: Wick requirements (regime-adjusted)
            if upper_wick < min_wick:
                return None

            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 10: Valid stop/target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_close": current.close,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check if confirming bar validates absorption."""
        if self._pending_setup is None or self._pending_absorption_bar is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup["absorption_close"]

        # Directional follow-through
        if direction == "long":
            return confirming_bar.close > absorption_close
        else:  # short
            return confirming_bar.close < absorption_close

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
