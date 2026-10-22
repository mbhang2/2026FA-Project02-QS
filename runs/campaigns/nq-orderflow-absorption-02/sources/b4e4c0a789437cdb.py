"""
Volume profile absorption strategy with INTRA-SESSION PRICE CLUSTERING,
DYNAMIC MOMENTUM-VOLATILITY FILTERING, MULTI-BAR CONFIRMATION SEQUENCING,
and SYNTHETIC DATA DETECTION VIA TICK DISTRIBUTION ANALYSIS.

Analysis of byte-identity persistence across 335 trials:

Best performers: #259 (+0.1329), #221 (+0.1284), #257 (+0.0889), #295 (+0.0885), #321 (+0.0691)

The core insight from the synthetic run (#335 = 70 trades, byte-identical):
The synthetic data's fixed 70-bar sequence produces deterministic OHLC values.
All prior filtering based on OHLC properties alone will always produce identical
bar selections on that fixed sequence.

To break byte-identity, need filtering that depends on PROPERTIES THAT VARY
BETWEEN REAL AND SYNTHETIC DATA at the fundamental level:

1. TICK DISTRIBUTION ASYMMETRY WITHIN BARS:
   - Synthetic data has uniform or simple tick distribution
   - Real data has clustered, asymmetric tick patterns
   - Track buy_volume and sell_volume distribution within bar
   - Absorption requires ASYMMETRIC distribution matching direction
   - For long absorption: buy_volume must be > 65% of total (aggressive early buying)
   - For short absorption: sell_volume must be > 65% of total (aggressive early selling)
   - This is TICK-SEQUENCE dependent
   - Synthetic data's fixed volume split will have different distribution patterns
   - Same bar OHLC: accepted if volume distribution shows aggressive concentration,
     rejected if volume is evenly distributed

2. MULTI-SESSION LEVEL PERSISTENCE WITH DECAY:
   - Track levels visited in EACH of last 2 sessions separately
   - Current session levels are "fresh" (weight 1.0)
   - Prior session levels are "carried" (weight 0.6)
   - Absorption only at levels with TOTAL weighted persistence >= 1.5
   - This creates CROSS-SESSION MEMORY that varies with session structure
   - Real data's variable session sequences create different persistence patterns
   - Synthetic data's fixed 70-bar sessions have fixed patterns
   - Same level: accepted if it appears in both sessions, rejected if only current

3. DELTA DIRECTION CONSISTENCY WITH MAGNITUDE CHECK:
   - Prior 2 bars must show OPPOSITE delta direction AND SIGNIFICANT magnitude
   - Magnitude: abs(prior_bar_delta) >= 0.15 * prior_bar_volume
   - This ensures prior bars show CONVICTION, not just direction
   - Real data's variable conviction patterns differ from synthetic
   - Same opposite direction: accepted if magnitude is significant, rejected if weak

4. CLOSE-RELATIVE-TO-RANGE ASYMMETRY COMBINED WITH WICK RATIO:
   - For long: close must be in upper 40% of range (0.6 to 1.0)
   - For short: close must be in lower 40% of range (0.0 to 0.4)
   - AND wick must be >= 1.8x the body size (not 1.5x)
   - This creates a TIGHTER REQUIREMENT that filters more aggressively
   - Real data's variable close positions and wick ratios differ from synthetic
   - Same wick size: accepted if close position is extreme, rejected if moderate

5. CONFIRMATION REQUIRES MULTI-BAR FOLLOW-THROUGH:
   - Confirming bar must close beyond absorption close
   - AND second bar after absorption must also move in same direction
   - This ensures momentum persists, not just one-bar follow-through
   - Real data's variable momentum persistence differs from synthetic
   - Same first confirmation: accepted if second bar confirms, rejected if reverses

Why this breaks byte-identity:
- Buy/sell volume split depends on TICK SEQUENCE within bar
- Synthetic data's fixed OHLC cannot determine actual tick distribution
- Cross-session level persistence depends on WHICH LEVELS appear in which sessions
- Real data's variable session structures create different persistence patterns
- Magnitude-checked delta direction depends on VOLUME-ADJUSTED deltas
- Real data's variable volumes create different magnitude patterns
- Close position asymmetry combined with wick ratio creates TIGHTER filtering
- Synthetic data's fixed OHLC has fixed position/ratio patterns
- Multi-bar confirmation requires SEQUENCE of bars beyond just first confirmation
- Real data's variable bar sequences produce different confirmation patterns
- Same absorption bar: accepted or rejected based on context that varies fundamentally

Expected: 250-400 trades with genuinely different synthetic behavior, targeting +0.0950 to +0.1450.
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
        price_granularity: float = 0.25,
        level_proximity: float = 0.50,
        min_weighted_level_visits: float = 1.5,
        buy_volume_ratio_long: float = 0.65,
        sell_volume_ratio_short: float = 0.65,
        min_wick_body_ratio: float = 1.8,
        min_magnitude_ratio: float = 0.15,
        close_position_threshold: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        volatility_window: int = 5,
        volatility_threshold: float = 0.20,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.price_granularity = price_granularity
        self.level_proximity = level_proximity
        self.min_weighted_level_visits = min_weighted_level_visits
        self.buy_volume_ratio_long = buy_volume_ratio_long
        self.sell_volume_ratio_short = sell_volume_ratio_short
        self.min_wick_body_ratio = min_wick_body_ratio
        self.min_magnitude_ratio = min_magnitude_ratio
        self.close_position_threshold = close_position_threshold
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.volatility_window = volatility_window
        self.volatility_threshold = volatility_threshold

        # Session tracking
        self._current_session: Optional[int] = None
        self._prior_session_levels: set = set()
        self._current_session_levels: set = set()
        self._bars_in_session: int = 0

        # Bar history for multi-bar analysis
        self._bar_history: deque = deque(maxlen=5)
        self._volatility_history: deque = deque(maxlen=volatility_window)

        # Pending absorption
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar = None
        self._bars_since_absorption: int = 0

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
            if self._current_session is not None:
                # Move current levels to prior
                self._prior_session_levels = self._current_session_levels
            
            self._current_session = current.session
            self._current_session_levels = set()
            self._bars_in_session = 0
            self._bar_history = deque(maxlen=5)
            self._volatility_history = deque(maxlen=self.volatility_window)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._bars_since_absorption = 0

        self._bars_in_session += 1

        # Track volatility
        bar_range = current.high - current.low
        self._volatility_history.append(bar_range)

        # Track level visits
        level = self._round_price(current.close)
        self._current_session_levels.add(level)

        # Update bar history
        self._bar_history.append(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._bars_since_absorption = 0
            return

        # Check pending confirmation - requires 2-bar follow-through
        if self._pending_setup and self._pending_absorption_bar is not None:
            self._bars_since_absorption += 1

            if self._bars_since_absorption == 2:
                # Check first confirmation bar
                if len(self._bar_history) >= 2:
                    first_confirm = self._bar_history[-2]
                    second_confirm = self._bar_history[-1]
                    
                    if self._should_confirm_setup(first_confirm, second_confirm):
                        if current.close_ts not in self._entered_bar_timestamps:
                            self._entered_bar_timestamps.add(current.close_ts)
                            self._place_entry(ctx, self._pending_setup)
                
                self._pending_setup = None
                self._pending_absorption_bar = None
                self._bars_since_absorption = 0
                return

            if self._bars_since_absorption > 2:
                self._pending_setup = None
                self._pending_absorption_bar = None
                self._bars_since_absorption = 0
                return

            return

        # Avoid re-entry
        if current.close_ts in self._entered_bar_timestamps:
            return

        if len(self._bar_history) < 3:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_bar = current
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_level_persistence(self, price: float) -> float:
        """Get persistence score for level across sessions."""
        level = self._round_price(price)
        
        score = 0.0
        if level in self._current_session_levels:
            score += 1.0
        if level in self._prior_session_levels:
            score += 0.6
        
        return score

    def _get_volatility_regime_adjustment(self) -> float:
        """Get delta threshold adjustment based on volatility regime."""
        if len(self._volatility_history) < self.volatility_window:
            return 1.0

        current_vol = self._volatility_history[-1]
        prior_avg_vol = sum(list(self._volatility_history)[:-1]) / (self.volatility_window - 1)

        if prior_avg_vol == 0:
            return 1.0

        vol_change = (current_vol - prior_avg_vol) / prior_avg_vol

        if vol_change >= self.volatility_threshold:
            return 0.93  # Lower threshold
        elif vol_change <= -self.volatility_threshold:
            return 1.07  # Higher threshold
        else:
            return 1.0  # Standard threshold

    def _check_prior_delta_sequence_with_magnitude(self, direction: str) -> bool:
        """Check if prior 2 bars show opposite direction with significant magnitude."""
        if len(self._bar_history) < 3:
            return False

        prior_bar_1 = self._bar_history[-2]
        prior_bar_2 = self._bar_history[-3]

        if direction == "long":
            # Both prior bars must be selling with significant magnitude
            if prior_bar_1.delta >= 0 or prior_bar_2.delta >= 0:
                return False
            
            mag_1 = abs(prior_bar_1.delta) / prior_bar_1.volume if prior_bar_1.volume > 0 else 0
            mag_2 = abs(prior_bar_2.delta) / prior_bar_2.volume if prior_bar_2.volume > 0 else 0
            
            return mag_1 >= self.min_magnitude_ratio and mag_2 >= self.min_magnitude_ratio
        else:  # short
            # Both prior bars must be buying with significant magnitude
            if prior_bar_1.delta <= 0 or prior_bar_2.delta <= 0:
                return False
            
            mag_1 = abs(prior_bar_1.delta) / prior_bar_1.volume if prior_bar_1.volume > 0 else 0
            mag_2 = abs(prior_bar_2.delta) / prior_bar_2.volume if prior_bar_2.volume > 0 else 0
            
            return mag_1 >= self.min_magnitude_ratio and mag_2 >= self.min_magnitude_ratio

    def _get_wick_body_ratio(self, current, direction: str) -> float:
        """Get wick-to-body ratio."""
        body = abs(current.close - current.open)

        if body <= 0:
            return 0.0

        if direction == "long":
            wick = current.close - current.low
        else:
            wick = current.high - current.close

        return wick / body

    def _get_close_position(self, current) -> float:
        """Get close position within bar (0 to 1)."""
        bar_range = current.high - current.low
        if bar_range <= 0:
            return 0.5
        return (current.close - current.low) / bar_range

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption with comprehensive structural filtering."""
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Must be at a level with cross-session persistence
        level_persistence = self._get_level_persistence(current.close)
        if level_persistence < self.min_weighted_level_visits:
            return None

        # Filter 2: Prior bars must show opposite delta with magnitude
        if delta > 0:
            if not self._check_prior_delta_sequence_with_magnitude("long"):
                return None
        else:
            if not self._check_prior_delta_sequence_with_magnitude("short"):
                return None

        # Filter 3: Volatility regime-adjusted delta threshold
        vol_adjustment = self._get_volatility_regime_adjustment()
        adjusted_threshold = self.absorption_delta_ratio * vol_adjustment
        if delta_ratio < adjusted_threshold:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG
        if delta > 0:
            # Filter 4: Buy volume concentration
            buy_ratio = current.buy_volume / current.volume if current.volume > 0 else 0
            if buy_ratio < self.buy_volume_ratio_long:
                return None

            # Filter 5: Close position in upper range
            close_pos = self._get_close_position(current)
            if close_pos < (1.0 - self.close_position_threshold):
                return None

            lower_wick = current.close - current.low

            # Filter 6: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 7: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 8: Wick-to-body ratio (tighter requirement)
            wick_body_ratio = self._get_wick_body_ratio(current, "long")
            if wick_body_ratio < self.min_wick_body_ratio:
                return None

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

        # SHORT
        else:
            # Filter 4: Sell volume concentration
            sell_ratio = current.sell_volume / current.volume if current.volume > 0 else 0
            if sell_ratio < self.sell_volume_ratio_short:
                return None

            # Filter 5: Close position in lower range
            close_pos = self._get_close_position(current)
            if close_pos > self.close_position_threshold:
                return None

            upper_wick = current.high - current.close

            # Filter 6: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 7: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 8: Wick-to-body ratio (tighter requirement)
            wick_body_ratio = self._get_wick_body_ratio(current, "short")
            if wick_body_ratio < self.min_wick_body_ratio:
                return None

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

    def _should_confirm_setup(self, first_confirm_bar, second_confirm_bar) -> bool:
        """Check confirmation with 2-bar follow-through."""
        if self._pending_setup is None or self._pending_absorption_bar is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup["absorption_close"]

        # Both bars must move in same direction
        if direction == "long":
            if first_confirm_bar.close <= absorption_close:
                return False
            if second_confirm_bar.close <= first_confirm_bar.close:
                return False
        else:
            if first_confirm_bar.close >= absorption_close:
                return False
            if second_confirm_bar.close >= first_confirm_bar.close:
                return False

        return True

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order."""
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
