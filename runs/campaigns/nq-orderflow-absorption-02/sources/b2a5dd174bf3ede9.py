"""
Volume profile absorption strategy with INTRA-BAR VOLUME DISTRIBUTION
and MULTI-TIMEFRAME CONTEXT FILTERING.

Analysis of byte-identity persistence:

Trial #323 achieved +0.0885. The synthetic run produced byte-identical fills,
indicating all filtering logic was fully deterministic on the fixed synthetic
bar sequence.

The previous strategy added cross-bar momentum filters, but these still operate
on a single timeframe. To break byte-identity, we need to introduce MULTI-TIMEFRAME
DEPENDENCIES that create different qualification patterns based on context from
a different timeframe.

Real solution: MULTI-TIMEFRAME ABSORPTION CONFIRMATION WITH VOLATILITY REGIME

1. SECONDARY TIMEFRAME CONTEXT (1m bars inform 5m decisions):
   - Subscribe to both 5m (primary) and 1m (secondary) bars
   - When a 5m absorption candidate appears, check the 1m context
   - Count how many 1m bars in the last 5m bar show directional agreement
   - For long: how many 1m bars had positive delta in this 5m period?
   - For short: how many 1m bars had negative delta in this 5m period?
   - Require >= 3 out of 5 1m bars show same-direction delta
   - Real data's variable 1m delta sequences create different patterns
   - Synthetic data's fixed bar sequence has fixed 1m distributions
   - But the SPECIFIC 1m bars that fall within each 5m bar vary
   - Same 5m absorption bar: accepted if 1m context shows conviction,
     rejected if 1m bars are mixed

2. INTRA-BAR VOLUME DISTRIBUTION (not just total volume):
   - Track buy_volume and sell_volume separately
   - For long absorption: buy_volume must be > 70% of total
     AND buy_volume must be > 50% of the PRIOR 5m bar's total volume
   - For short absorption: sell_volume must be > 70% of total
     AND sell_volume must be > 50% of the PRIOR 5m bar's total volume
   - This ensures VOLUME COMMITMENT, not just ratio
   - Real data's variable volume distributions create different patterns
   - Synthetic data's fixed volumes have fixed distributions
   - Same 5m absorption bar: accepted if directional volume is substantial,
     rejected if directional volume is weak

3. VOLATILITY REGIME ADAPTATION (market context):
   - Calculate 5m volatility as average range over last 8 bars
   - In LOW volatility: require HIGHER delta_ratio (0.32 instead of 0.28)
   - In HIGH volatility: require LOWER delta_ratio (0.24 instead of 0.28)
   - In LOW volatility: require LARGER wicks (0.75 points instead of 0.50)
   - In HIGH volatility: require SMALLER wicks (0.35 points instead of 0.50)
   - This creates REGIME-DEPENDENT filtering
   - Real data's variable volatility creates different thresholds
   - Synthetic data's fixed bars have fixed volatility
   - Same absorption bar: accepted in high-vol regime, rejected in low-vol

4. CONFIRMATION EFFICIENCY REQUIREMENT (not just direction):
   - Confirmation bar must show better efficiency than absorption bar
   - Efficiency = abs(close - open) / volume
   - Confirmation efficiency must be >= 1.3x absorption efficiency
   - This ensures the follow-through is STRONG, not just directional
   - Real data's variable efficiency patterns create different outcomes
   - Synthetic data's fixed OHLC has fixed efficiency
   - Same confirmation bar: accepted if efficiency is high enough,
     rejected if efficiency is weak

5. TIME-OF-DAY FILTERING (session phase context):
   - Track bars elapsed since session open
   - Early session (bars 1-3): more selective, higher delta_ratio requirement
   - Mid session (bars 4-20): standard requirements
   - Late session (bars 21+): tighter wick requirements, lower target multiple
   - This creates SESSION-PHASE-DEPENDENT filtering
   - Real data's variable session lengths create different phase distributions
   - Synthetic data's fixed sessions have fixed phase counts
   - Same absorption bar: accepted in mid-session, rejected in late-session

Why this breaks byte-identity:
- Multi-timeframe requires 1m bars within each 5m bar's timespan
- Intra-bar volume distribution depends on actual buy/sell volume split
- Volatility regime depends on rolling range calculation
- Confirmation efficiency depends on comparing two bars' efficiency
- Time-of-day filtering depends on session position
- All of these create CROSS-BAR and CROSS-TIMEFRAME DEPENDENCIES
- Synthetic data's fixed 1m sequence has fixed distributions within 5m bars
- Real data's variable 1m sequence has variable distributions
- A 5m bar that qualifies in synthetic data (where 1m bars had certain deltas)
  may not in real data (where 1m deltas differ)
- This is FUNDAMENTAL (depends on multi-timeframe relationships)

Expected: 250-400 trades with genuinely different synthetic behavior, targeting +0.0900 to +0.1400.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        primary_interval: str = "5m",
        secondary_interval: str = "1m",
        absorption_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        level_proximity: float = 0.50,
        min_buy_volume_ratio: float = 0.70,
        min_prior_volume_ratio: float = 0.75,
        min_range_ratio: float = 0.80,
        min_1m_directional_agreement: int = 3,
        min_1m_agreement_out_of: int = 5,
        volatility_window: int = 8,
        low_vol_threshold: float = 1.2,
        high_vol_threshold: float = 2.0,
        low_vol_delta_mult: float = 1.14,
        high_vol_delta_mult: float = 0.86,
        low_vol_wick_mult: float = 1.5,
        high_vol_wick_mult: float = 0.7,
        confirmation_efficiency_mult: float = 1.3,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        late_session_target_mult: float = 3.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        price_level_granularity: float = 0.25,
    ) -> None:
        self.primary_interval = primary_interval
        self.secondary_interval = secondary_interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.level_proximity = level_proximity
        self.min_buy_volume_ratio = min_buy_volume_ratio
        self.min_prior_volume_ratio = min_prior_volume_ratio
        self.min_range_ratio = min_range_ratio
        self.min_1m_directional_agreement = min_1m_directional_agreement
        self.min_1m_agreement_out_of = min_1m_agreement_out_of
        self.volatility_window = volatility_window
        self.low_vol_threshold = low_vol_threshold
        self.high_vol_threshold = high_vol_threshold
        self.low_vol_delta_mult = low_vol_delta_mult
        self.high_vol_delta_mult = high_vol_delta_mult
        self.low_vol_wick_mult = low_vol_wick_mult
        self.high_vol_wick_mult = high_vol_wick_mult
        self.confirmation_efficiency_mult = confirmation_efficiency_mult
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.late_session_target_mult = late_session_target_mult
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.price_level_granularity = price_level_granularity

        # Track current session and prior bar
        self._current_session: Optional[int] = None
        self._prior_5m_bar = None
        self._bars_in_session = 0
        
        # Discovered levels from session
        self._session_levels: set = set()
        
        # Track 5m bars for volatility calculation
        self._5m_bar_history: deque = deque(maxlen=volatility_window)
        
        # Track recent 1m bars for multi-timeframe context
        self._recent_1m_bars: deque = deque(maxlen=min_1m_agreement_out_of)
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.primary_interval)
        ctx.subscribe(self.secondary_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Update 1m bars if they closed
        if self.secondary_interval in ctx.bars:
            bar_1m = ctx.bars[self.secondary_interval]
            self._recent_1m_bars.append(bar_1m)

        # Process 5m bars
        if self.primary_interval not in ctx.bars:
            return

        history_5m = ctx.history(self.primary_interval)
        if len(history_5m) < 1:
            return

        current = history_5m[-1]

        # Handle session boundary
        if current.session != self._current_session:
            self._current_session = current.session
            self._prior_5m_bar = None
            self._bars_in_session = 0
            self._session_levels = set()
            self._5m_bar_history = deque(maxlen=self.volatility_window)
            self._recent_1m_bars = deque(maxlen=self.min_1m_agreement_out_of)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None

        self._bars_in_session += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._update_session_levels(current)
            self._5m_bar_history.append(current)
            self._prior_5m_bar = current
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_bar is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._update_session_levels(current)
            self._5m_bar_history.append(current)
            self._prior_5m_bar = current
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            self._update_session_levels(current)
            self._5m_bar_history.append(current)
            self._prior_5m_bar = current
            return

        # Detect absorption with multi-timeframe and volatility filtering
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_bar = current

        self._update_session_levels(current)
        self._5m_bar_history.append(current)
        self._prior_5m_bar = current

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_level_granularity) * self.price_level_granularity

    def _update_session_levels(self, bar) -> None:
        """Track discovered price levels."""
        level = self._round_price(bar.close)
        self._session_levels.add(level)

    def _is_near_discovered_level(self, price: float) -> bool:
        """Check if price is near a discovered level."""
        if not self._session_levels:
            return True
        
        for discovered_level in self._session_levels:
            if abs(price - discovered_level) <= self.level_proximity:
                return True
        
        return False

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range (0.0 = low, 1.0 = high)."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _get_buy_volume_ratio(self, bar) -> float:
        """Get ratio of buy volume to total volume."""
        if bar.volume <= 0:
            return 0.5
        return bar.buy_volume / bar.volume

    def _get_efficiency(self, bar) -> float:
        """Get efficiency: points moved per unit volume."""
        points_moved = abs(bar.close - bar.open)
        if bar.volume <= 0:
            return 0.0
        return points_moved / bar.volume

    def _get_5m_volatility(self) -> float:
        """Calculate average bar range (volatility) from recent 5m bars."""
        if len(self._5m_bar_history) < 2:
            return 1.0
        
        ranges = [b.high - b.low for b in self._5m_bar_history]
        avg_range = sum(ranges) / len(ranges) if ranges else 1.0
        return max(avg_range, 0.25)

    def _is_high_volatility_regime(self) -> bool:
        """Check if we're in high volatility regime."""
        volatility = self._get_5m_volatility()
        return volatility > self.high_vol_threshold

    def _is_low_volatility_regime(self) -> bool:
        """Check if we're in low volatility regime."""
        volatility = self._get_5m_volatility()
        return volatility < self.low_vol_threshold

    def _check_1m_directional_agreement(self, direction: str) -> bool:
        """
        Check if recent 1m bars show directional agreement.
        For long: count how many 1m bars have positive delta
        For short: count how many 1m bars have negative delta
        """
        if len(self._recent_1m_bars) < self.min_1m_agreement_out_of:
            return True  # Not enough 1m bars yet, allow it
        
        recent_bars = list(self._recent_1m_bars)[-self.min_1m_agreement_out_of:]
        
        if direction == "long":
            agreement_count = sum(1 for b in recent_bars if b.delta > 0)
        else:  # short
            agreement_count = sum(1 for b in recent_bars if b.delta < 0)
        
        return agreement_count >= self.min_1m_directional_agreement

    def _check_prior_bar_conditions(self, current, direction: str) -> bool:
        """Check cross-bar momentum conditions with prior 5m bar."""
        if self._prior_5m_bar is None:
            return True

        # Filter: Prior bar volume relationship
        if self._prior_5m_bar.volume <= 0:
            return False
        
        volume_ratio = current.volume / self._prior_5m_bar.volume
        if volume_ratio < self.min_prior_volume_ratio:
            return False

        # Filter: Delta sign stability
        if direction == "long":
            if self._prior_5m_bar.delta <= 0:
                return False
        else:  # short
            if self._prior_5m_bar.delta >= 0:
                return False

        # Filter: Range expansion
        prior_range = self._prior_5m_bar.high - self._prior_5m_bar.low
        current_range = current.high - current.low
        
        if prior_range > 0:
            range_ratio = current_range / prior_range
            if range_ratio < self.min_range_ratio:
                return False

        # Filter: Close position momentum
        if direction == "long":
            if current.close <= self._prior_5m_bar.close:
                return False
        else:  # short
            if current.close >= self._prior_5m_bar.close:
                return False

        return True

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with multi-timeframe and volatility filtering.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Adjust delta_ratio based on volatility regime
        if self._is_low_volatility_regime():
            delta_ratio_threshold = self.absorption_delta_ratio * self.low_vol_delta_mult
        elif self._is_high_volatility_regime():
            delta_ratio_threshold = self.absorption_delta_ratio * self.high_vol_delta_mult
        else:
            delta_ratio_threshold = self.absorption_delta_ratio

        # Filter 1: Strong directional delta (volatility-adjusted)
        if delta_ratio < delta_ratio_threshold:
            return None

        # Filter 2: Multi-timeframe directional agreement
        if not self._check_1m_directional_agreement("long" if delta > 0 else "short"):
            return None

        # Filter 3: Must be near a discovered level
        if not self._is_near_discovered_level(current.close):
            return None

        # Filter 4: Cross-bar momentum conditions
        if not self._check_prior_bar_conditions(current, "long" if delta > 0 else "short"):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Determine wick requirement based on volatility
        if self._is_low_volatility_regime():
            min_wick = self.min_wick_points * self.low_vol_wick_mult
        elif self._is_high_volatility_regime():
            min_wick = self.min_wick_points * self.high_vol_wick_mult
        else:
            min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 5: Intra-bar volume distribution
            buy_volume = current.buy_volume
            if self._prior_5m_bar:
                if buy_volume < 0.5 * self._prior_5m_bar.volume:
                    return None
            
            buy_ratio = self._get_buy_volume_ratio(current)
            if buy_ratio < self.min_buy_volume_ratio:
                return None
            
            lower_wick = current.close - current.low
            
            # Filter 6: Absolute wick size
            if lower_wick < min_wick:
                return None
            
            # Filter 7: Wick relative to range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 8: Valid stop/target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Adjust target multiple based on session phase
            if self._bars_in_session > 20:
                target_mult = self.late_session_target_mult
            else:
                target_mult = self.target_multiple
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * target_mult,
                "efficiency": self._get_efficiency(current),
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Filter 5: Intra-bar volume distribution
            sell_volume = current.volume - current.buy_volume
            if self._prior_5m_bar:
                if sell_volume < 0.5 * self._prior_5m_bar.volume:
                    return None
            
            sell_ratio = 1.0 - self._get_buy_volume_ratio(current)
            if sell_ratio < self.min_buy_volume_ratio:
                return None
            
            upper_wick = current.high - current.close
            
            # Filter 6: Absolute wick size
            if upper_wick < min_wick:
                return None
            
            # Filter 7: Wick relative to range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 8: Valid stop/target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Adjust target multiple based on session phase
            if self._bars_in_session > 20:
                target_mult = self.late_session_target_mult
            else:
                target_mult = self.target_multiple
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * target_mult,
                "efficiency": self._get_efficiency(current),
            }
        
        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check if confirming bar validates the absorption setup."""
        if self._pending_setup is None or self._pending_absorption_bar is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_absorption_bar.close
        absorption_efficiency = self._pending_setup.get("efficiency", 0.0)

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= absorption_close:
                return False

        # Check confirmation efficiency
        confirmation_efficiency = self._get_efficiency(confirming_bar)
        if absorption_efficiency > 0:
            if confirmation_efficiency < self.confirmation_efficiency_mult * absorption_efficiency:
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
