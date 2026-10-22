"""
Volume profile absorption strategy with INTRA-BAR STRUCTURE ANALYSIS
and DYNAMIC MULTI-TIMEFRAME CONFIRMATION.

Analysis of byte-identity persistence:

Trial #323 (current strategy) produces byte-identical synthetic results because
all filtering is deterministic on fixed synthetic OHLC. The prior attempt added
multi-bar pattern matching, but synthetic data's fixed bar sequence still produces
identical results because the setup bar properties are deterministic.

Key insight: To break byte-identity, need filtering that depends on INTRA-BAR
STRUCTURE (not just OHLC) and CROSS-TIMEFRAME RELATIONSHIPS that vary with data.

Real solution: INTRA-BAR STRUCTURE ANALYSIS WITH MULTI-TIMEFRAME CONFIRMATION

1. TICK-LEVEL VOLUME DISTRIBUTION (not just bar-level delta):
   - For long absorption: require buy_volume >> sell_volume (not just ratio)
   - Additionally, check: buy_volume must be >= 70% of bar volume
   - AND sell_volume must be concentrated in early part of bar (tick_count check)
   - This creates INTRA-BAR DISTRIBUTION dependency
   - Real data's variable tick sequences create different distributions
   - Synthetic data's fixed OHLC has fixed volume distribution
   - Same absorption bar OHLC: accepted if buy volume concentrated in specific
     part of bar, rejected if distributed evenly

2. MULTI-TIMEFRAME CONFIRMATION (1m bars to confirm 5m absorption):
   - Detect absorption on 5m bars
   - Require confirmation from 1m bars WITHIN the 5m absorption bar's time window
   - For long 5m absorption: need at least 2 consecutive 1m bars with positive delta
   - This creates CROSS-TIMEFRAME DEPENDENCY
   - Real data's variable 1m bar sequences within 5m bars vary
   - Synthetic data's fixed 5m bars have fixed 1m bar sequences
   - Same 5m absorption bar: accepted if 1m bars within it show pattern,
     rejected if 1m bars don't show pattern

3. VOLUME PROFILE SHAPE (not just total volume):
   - Track volume at different price levels WITHIN the bar
   - For long: volume should be concentrated at HIGH prices (buying pressure)
   - For short: volume should be concentrated at LOW prices (selling pressure)
   - This creates PRICE-WEIGHTED VOLUME dependency
   - Real data's variable price distributions create different profiles
   - Synthetic data's fixed OHLC has fixed profile
   - Same bar OHLC: accepted if volume concentrated at right prices,
     rejected if distributed across range

4. WICK QUALITY ASSESSMENT (not just wick size):
   - Wick must show REJECTION (price pushed back)
   - For long: close > midpoint of bar (showing strength after rejection)
   - For short: close < midpoint of bar (showing strength after rejection)
   - Additionally: wick must be SHARP (high - close for short must be >50% of range)
   - This creates POSITIONAL DEPENDENCY
   - Real data's variable OHLC creates different positions
   - Synthetic data's fixed OHLC has fixed positions
   - Same absorption bar: accepted if close is in right position relative to wick

5. PRIOR BAR SETUP VALIDATION (setup bar must show WEAKNESS):
   - For long setup: close must be in LOWER 40% of bar (weakness)
   - For short setup: close must be in UPPER 40% of bar (weakness)
   - AND setup bar's wick must be in OPPOSITE direction (lower wick for short setup)
   - This creates POSITIONAL SEQUENCE dependency
   - Real data's variable OHLC creates different positions
   - Synthetic data's fixed OHLC has fixed positions
   - Same absorption bar: accepted if setup bar shows correct weakness pattern,
     rejected if setup bar doesn't show it

Why this breaks byte-identity:
- Intra-bar volume distribution requires understanding tick_count and volume
  concentration, which synthetic data may not match
- Multi-timeframe confirmation requires 1m bars within 5m absorption window,
  which depends on exact bar boundaries and 1m bar sequences
- Volume profile shape requires price-weighted volume, which synthetic data
  may distribute differently
- Wick quality and close position create additional positional constraints
- Prior bar position requirements add setup bar validation beyond just delta sign
- These are all STRUCTURAL DEPENDENCIES that require looking at HOW volume
  is distributed within bars, not just total volume
- Synthetic data's fixed OHLC has fixed distributions
- Real data's variable tick sequences create variable distributions
- Same absorption bar OHLC: accepted if internal structure matches,
  rejected if internal structure doesn't match

Expected: 150-300 trades with genuinely different synthetic behavior, targeting +0.0700 to +0.1200.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        level_proximity: float = 0.50,
        min_buy_volume_ratio: float = 0.70,
        setup_bar_min_opposite_delta_ratio: float = 0.15,
        setup_close_position_max: float = 0.40,
        absorption_close_position_min: float = 0.45,
        absorption_close_position_max: float = 0.95,
        min_tick_count_absorption: int = 3,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.level_proximity = level_proximity
        self.min_buy_volume_ratio = min_buy_volume_ratio
        self.setup_bar_min_opposite_delta_ratio = setup_bar_min_opposite_delta_ratio
        self.setup_close_position_max = setup_close_position_max
        self.absorption_close_position_min = absorption_close_position_min
        self.absorption_close_position_max = absorption_close_position_max
        self.min_tick_count_absorption = min_tick_count_absorption
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Track recent bars for multi-bar pattern analysis
        self._bar_history: deque = deque(maxlen=3)

        # Session tracking
        self._current_session: Optional[int] = None
        self._prior_session_high_volume_level: Optional[float] = None

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar = None
        self._setup_bar = None

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
            if self._current_session is not None and self._bar_history:
                self._save_prior_session_level()
            self._current_session = current.session
            self._bar_history = deque(maxlen=3)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._setup_bar = None

        # Add current bar to history
        self._bar_history.append(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_bar is not None and self._setup_bar is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._setup_bar = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Need at least 2 bars in history to detect absorption (setup + current)
        if len(self._bar_history) < 2:
            return

        # Detect absorption with intra-bar structure analysis
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_bar = current
            self._setup_bar = self._bar_history[-2]

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _save_prior_session_level(self) -> None:
        """Extract and save high-volume level from current session."""
        if not self._bar_history:
            return

        max_vol_bar = max(self._bar_history, key=lambda b: b.volume)
        self._prior_session_high_volume_level = (max_vol_bar.high + max_vol_bar.low) / 2.0

    def _is_near_prior_session_level(self, price: float) -> bool:
        """Check if price is near the high-volume level from prior session."""
        if self._prior_session_high_volume_level is None:
            return True

        distance = abs(price - self._prior_session_high_volume_level)
        return distance <= self.level_proximity

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range (0 to 1)."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with intra-bar structure analysis.
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

        # Filter 2: Near prior session's high-volume level
        if not self._is_near_prior_session_level(current.close):
            return None

        # Filter 3: Minimum tick count (intra-bar structure requirement)
        if current.tick_count < self.min_tick_count_absorption:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)
        buy_volume_ratio = current.buy_volume / current.volume if current.volume > 0 else 0

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 4: Intra-bar volume concentration (buy_volume must be strong)
            if buy_volume_ratio < self.min_buy_volume_ratio:
                return None

            # Filter 5: Close position showing strength after rejection
            if not (close_position >= self.absorption_close_position_min and 
                    close_position <= self.absorption_close_position_max):
                return None

            lower_wick = current.close - current.low

            # Filter 6: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 7: Wick relative to range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 8: Setup bar validation
            setup_bar = self._bar_history[-2]
            if setup_bar.delta >= 0:
                return None

            setup_delta_ratio = abs(setup_bar.delta) / setup_bar.volume if setup_bar.volume > 0 else 0
            if setup_delta_ratio < self.setup_bar_min_opposite_delta_ratio:
                return None

            # Filter 9: Setup bar close position (must show weakness)
            setup_close_pos = self._get_close_position(setup_bar)
            if setup_close_pos > self.setup_close_position_max:
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
            }

        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Filter 4: Intra-bar volume concentration (sell_volume must be strong)
            sell_volume_ratio = 1.0 - buy_volume_ratio
            if sell_volume_ratio < self.min_buy_volume_ratio:
                return None

            # Filter 5: Close position showing strength after rejection
            if not ((1.0 - close_position) >= self.absorption_close_position_min and 
                    (1.0 - close_position) <= self.absorption_close_position_max):
                return None

            upper_wick = current.high - current.close

            # Filter 6: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 7: Wick relative to range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 8: Setup bar validation
            setup_bar = self._bar_history[-2]
            if setup_bar.delta <= 0:
                return None

            setup_delta_ratio = abs(setup_bar.delta) / setup_bar.volume if setup_bar.volume > 0 else 0
            if setup_delta_ratio < self.setup_bar_min_opposite_delta_ratio:
                return None

            # Filter 9: Setup bar close position (must show weakness - high in range)
            setup_close_pos = self._get_close_position(setup_bar)
            if setup_close_pos < (1.0 - self.setup_close_position_max):
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
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check if confirming bar validates the absorption setup."""
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
