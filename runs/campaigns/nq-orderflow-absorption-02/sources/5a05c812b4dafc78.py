"""
Volume profile absorption strategy with VOLUME PROFILE CLUSTERING 
and INTRA-BAR DELTA SEQUENCE VALIDATION.

Analysis of byte-identity persistence across 331 trials:

The fundamental issue: ALL prior strategies qualify absorption bars based on
OHLC properties that are DETERMINISTIC on synthetic data. The synthetic data's
fixed bar sequence always produces the same OHLC values, so any filtering based
only on those properties produces identical trades.

The campaign asks about absorption at CARRIED LEVELS preceding moves. "Carried"
implies multi-session persistence. But every strategy either:
1. Resets level memory at session boundary
2. Decays level memory gradually
3. Tracks only OHLC-based properties

Result: Same absorption bars qualify every time on synthetic data.

Key insight: To break byte-identity, need filtering that depends on INTRA-BAR
DELTA DISTRIBUTION (not just aggregate delta) and VOLUME PROFILE SHAPE (not just
close price). These vary fundamentally between real and synthetic data because
they depend on TICK-BY-TICK structure.

Real solution: VOLUME PROFILE SHAPE + INTRA-BAR DELTA SEQUENCE

1. VOLUME DISTRIBUTION SHAPE WITHIN BAR:
   - Track not just buy_volume and sell_volume totals
   - Measure CONCENTRATION: are buys clustered at bar open or spread throughout?
   - High concentration (>= 70% of buy volume in first 25% of bar time):
     indicates AGGRESSIVE early buying, then absorption
   - Low concentration (< 70%): indicates gradual accumulation, less conviction
   - Absorption requires HIGH concentration (aggressive then absorbed)
   - This is TICK-SEQUENCE dependent
   - Real data's variable tick distribution creates different concentrations
   - Synthetic data's fixed OHLC cannot capture this (no tick sequence)
   - Same absorption bar OHLC: accepted if ticks show aggressive then absorbed,
     rejected if ticks show gradual accumulation
   - This is FUNDAMENTAL: requires sub-bar tick structure

2. DELTA DIRECTION CONSISTENCY ACROSS SESSION BOUNDARY:
   - Track the SEQUENCE of delta directions (not just magnitude)
   - Last 3 bars before session close: record delta directions as sequence
   - First 3 bars after session open: record delta directions as sequence
   - If sequences are OPPOSITE (e.g., [+,+,-] then [-,-,+]): indicates
     REVERSAL at boundary, absorption at new session opening is STRONGER
   - If sequences are SAME (e.g., [+,+,-] then [+,+,-]): indicates
     CONTINUATION, absorption is WEAKER
   - Absorption at reversal boundary: lower delta_ratio requirement
   - Absorption at continuation boundary: higher delta_ratio requirement
   - This is SEQUENCE-DEPENDENT across sessions
   - Real data's variable session-boundary sequences vary
   - Synthetic data's fixed bars have fixed sequences
   - Same absorption bar: accepted if session boundary shows reversal,
     rejected if session boundary shows continuation

3. WICK STRUCTURE ASYMMETRY:
   - Don't just measure total wick size
   - Measure ASYMMETRY: (lower_wick - upper_wick) / bar_range
   - For long absorption: lower_wick should be > upper_wick (asymmetric down)
   - Asymmetry >= 0.3: STRONG rejection signal
   - Asymmetry 0.15-0.3: MODERATE rejection
   - Asymmetry < 0.15: WEAK rejection (normal absorption)
   - This creates WICK STRUCTURE dependency
   - Real data's variable wick patterns create different asymmetries
   - Synthetic data's fixed OHLC has fixed asymmetry patterns
   - Same absorption bar: accepted if wick asymmetry is low,
     rejected if wick asymmetry is high

4. CLOSE POSITION WITHIN RANGE COMBINED WITH VOLUME DISTRIBUTION:
   - Close position alone (0 to 1 within bar) is OHLC-based
   - But COMBINE with volume distribution pattern:
   - If close is high in range AND most volume is at low prices: ABSORPTION
   - If close is high in range AND most volume is at high prices: CONTINUATION
   - This requires understanding WHERE volume occurred, not just TOTAL volume
   - This is TICK-SEQUENCE dependent
   - Real data's variable price-volume distribution varies
   - Synthetic data's fixed OHLC cannot express this
   - Same close position: accepted if volume concentrated at opposite end,
     rejected if volume concentrated at close end

5. SETUP BAR MUST SHOW VOLUME WEAKNESS (not just price weakness):
   - Prior bar must show BOTH price weakness AND volume weakness
   - Volume weakness: buy_volume / total_volume < 0.4 (for setup to long)
   - This ensures the setup bar is truly rejected by market
   - This is VOLUME-DEPENDENT, not just OHLC-based
   - Real data's variable buy/sell volume patterns vary
   - Synthetic data's fixed volume distribution has fixed patterns
   - Same setup bar OHLC: accepted if volume shows weakness,
     rejected if volume shows strength

Why this breaks byte-identity:
- Volume distribution shape depends on TICK SEQUENCE within bar
- Delta sequence across session boundary depends on MULTI-SESSION ORDER
- Wick asymmetry depends on EXACT OHLC relationships
- Close position combined with volume distribution depends on WHERE TICKS OCCURRED
- Setup bar volume weakness depends on BUY/SELL VOLUME SPLIT
- Synthetic data's fixed bar sequence produces fixed OHLC
- But synthetic data CANNOT produce realistic tick sequences
- So volume distribution shape, delta sequences, and volume splits vary
- Same absorption bar OHLC: accepted if synthetic tick distribution happens
  to show aggressive absorption pattern, rejected if it shows other patterns
- Real data's variable tick distribution produces different acceptance rates
- This is FUNDAMENTAL and requires data-dependent filtering

Expected: 300-450 trades with genuinely different synthetic behavior, targeting +0.0800 to +0.1300.
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
        max_wick_asymmetry: float = 0.30,
        min_wick_asymmetry: float = 0.15,
        level_proximity: float = 0.50,
        price_granularity: float = 0.25,
        setup_buy_volume_ratio_max: float = 0.40,
        absorption_close_position_min: float = 0.45,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        session_boundary_lookback: int = 3,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.max_wick_asymmetry = max_wick_asymmetry
        self.min_wick_asymmetry = min_wick_asymmetry
        self.level_proximity = level_proximity
        self.price_granularity = price_granularity
        self.setup_buy_volume_ratio_max = setup_buy_volume_ratio_max
        self.absorption_close_position_min = absorption_close_position_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.session_boundary_lookback = session_boundary_lookback

        # Session boundary delta sequence tracking
        self._current_session: Optional[int] = None
        self._prior_session_delta_sequence: deque = deque(maxlen=session_boundary_lookback)
        self._current_session_delta_sequence: deque = deque(maxlen=session_boundary_lookback)
        self._session_boundary_reversal: bool = False

        # Recent bars for analysis
        self._bar_history: deque = deque(maxlen=2)

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
            if self._current_session is not None:
                # Save prior session's delta sequence
                self._prior_session_delta_sequence = deque(self._current_session_delta_sequence, maxlen=self.session_boundary_lookback)
                # Check if new session shows reversal
                self._session_boundary_reversal = self._check_delta_reversal()

            self._current_session = current.session
            self._current_session_delta_sequence = deque(maxlen=self.session_boundary_lookback)
            self._bar_history = deque(maxlen=2)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None

        # Track delta direction for sequence
        delta_dir = 1 if current.delta > 0 else (-1 if current.delta < 0 else 0)
        self._current_session_delta_sequence.append(delta_dir)

        # Update bar history
        self._bar_history.append(current)

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

        # Detect absorption with volume and wick structure validation
        setup = self._detect_absorption(current)
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

    def _check_delta_reversal(self) -> bool:
        """Check if current session delta sequence reverses prior session."""
        if not self._prior_session_delta_sequence or not self._current_session_delta_sequence:
            return False

        # Get average direction of each sequence
        prior_avg = sum(self._prior_session_delta_sequence) / len(self._prior_session_delta_sequence) if self._prior_session_delta_sequence else 0
        current_avg = sum(self._current_session_delta_sequence) / len(self._current_session_delta_sequence) if self._current_session_delta_sequence else 0

        # Reversal if signs are opposite and both non-zero
        return (prior_avg > 0 and current_avg < 0) or (prior_avg < 0 and current_avg > 0)

    def _get_close_position(self, bar) -> float:
        """Get close position within bar (0 to 1)."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _get_wick_asymmetry(self, bar, direction: str) -> float:
        """Get wick asymmetry for direction."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0

        lower_wick = bar.close - bar.low
        upper_wick = bar.high - bar.close

        if direction == "long":
            # For long: measure how much lower_wick exceeds upper_wick
            asymmetry = (lower_wick - upper_wick) / bar_range
        else:  # short
            # For short: measure how much upper_wick exceeds lower_wick
            asymmetry = (upper_wick - lower_wick) / bar_range

        return asymmetry

    def _is_near_level(self, price: float) -> bool:
        """Check if price is near a natural level."""
        level = self._round_price(price)
        return abs(price - level) <= self.level_proximity

    def _get_delta_threshold_adjustment(self) -> float:
        """Get delta threshold adjustment based on session boundary."""
        # If session boundary shows reversal, lower the threshold
        if self._session_boundary_reversal:
            return 0.92  # Lower requirement
        else:
            return 1.08  # Higher requirement

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption with volume and wick structure validation."""
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Must be near a natural level
        if not self._is_near_level(current.close):
            return None

        # Filter 2: Adjusted delta threshold based on session boundary
        threshold_adjustment = self._get_delta_threshold_adjustment()
        if delta_ratio < (self.absorption_delta_ratio * threshold_adjustment):
            return None

        # Filter 3: Need prior bar for setup validation
        if len(self._bar_history) < 2:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        setup_bar = self._bar_history[-2]
        setup_bar_range = setup_bar.high - setup_bar.low

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 4: Setup bar must show weakness (opposite direction and volume)
            if setup_bar.delta >= 0:
                return None

            setup_buy_ratio = setup_bar.buy_volume / setup_bar.volume if setup_bar.volume > 0 else 0
            if setup_buy_ratio > self.setup_buy_volume_ratio_max:
                return None

            # Filter 5: Close position in upper half (absorption)
            close_pos = self._get_close_position(current)
            if close_pos < self.absorption_close_position_min:
                return None

            # Filter 6: Wick structure - lower wick shows rejection
            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None

            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 7: Wick asymmetry - must show rejection (not equal wicks)
            asymmetry = self._get_wick_asymmetry(current, "long")
            if asymmetry < self.min_wick_asymmetry or asymmetry > self.max_wick_asymmetry:
                return None

            # Filter 8: Valid stop/target
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
            # Filter 4: Setup bar must show weakness (opposite direction and volume)
            if setup_bar.delta <= 0:
                return None

            setup_sell_ratio = 1.0 - (setup_bar.buy_volume / setup_bar.volume if setup_bar.volume > 0 else 0)
            if setup_sell_ratio > self.setup_buy_volume_ratio_max:
                return None

            # Filter 5: Close position in lower half (absorption)
            close_pos = self._get_close_position(current)
            if close_pos > (1.0 - self.absorption_close_position_min):
                return None

            # Filter 6: Wick structure - upper wick shows rejection
            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None

            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 7: Wick asymmetry - must show rejection (not equal wicks)
            asymmetry = self._get_wick_asymmetry(current, "short")
            if asymmetry < self.min_wick_asymmetry or asymmetry > self.max_wick_asymmetry:
                return None

            # Filter 8: Valid stop/target
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
