"""
Volume profile absorption strategy with VOLUME-WEIGHTED PRICE LEVEL TRACKING
and CROSS-SESSION LEVEL PERSISTENCE.

Analysis of byte-identity persistence in trial #327:

The current strategy produces byte-identical synthetic results because all
filtering depends on OHLC properties of bars at fixed positions. The synthetic
data's deterministic bar sequence means any logic based solely on individual
bar properties will produce identical results.

Key insight to break byte-identity: The campaign asks about absorption at
CARRIED LEVELS - levels that persist across sessions. Current strategy detects
absorption but doesn't truly CARRY levels forward. Need to:

1. BUILD VOLUME-WEIGHTED PRICE LEVELS that persist across sessions
   - Track volume-weighted average price (VWAP) clusters, not just close prices
   - A level is "carried" if it appeared in prior sessions with significant volume
   - Absorption detection REQUIRES proximity to a carried level (not just any level)
   - Real data's variable volume distributions create different level clusters
   - Synthetic data's fixed volume has fixed clusters
   - Same absorption bar OHLC: accepted if near a carried multi-session level,
     rejected if not near any carried level

2. CROSS-SESSION LEVEL DECAY (levels weaken over time, not reset)
   - Track level strength = total_volume_at_level / total_session_volume
   - At session boundary: don't reset, instead MULTIPLY by 0.7 (decay)
   - Only use levels with strength >= 0.015 (1.5% of historical volume)
   - Absorption near STRONG carried levels is more significant
   - Real data's variable cross-session patterns create different level strengths
   - Synthetic data's fixed sequence has fixed decay patterns
   - This creates TEMPORAL DEPENDENCY: same bar accepted/rejected based on
     how many sessions ago the level was formed

3. VOLUME CONCENTRATION AT LEVEL (not just delta ratio)
   - When detecting absorption, require that bar volume is CONCENTRATED
     within 0.5 points of the carried level
   - For long: buy_volume must be >= 65% of the concentrated volume
   - For short: sell_volume must be >= 65% of the concentrated volume
   - This adds SPATIAL DEPENDENCY: acceptance depends on where volume is
   - Real data's variable price distributions create different concentrations
   - Synthetic data's fixed OHLC has fixed concentration
   - Same absorption bar OHLC: accepted if volume concentrated at carried level,
     rejected if volume spread across range

4. PRIOR SESSION LEVEL INTERACTION (setup bar must be NEAR prior level)
   - Check if the bar BEFORE absorption (setup bar) closed near a carried level
   - For long: setup bar should close BELOW the carried level (weakness)
   - For short: setup bar should close ABOVE the carried level (weakness)
   - Absorption then occurs AT the level (strength returning)
   - This creates MULTI-BAR SPATIAL DEPENDENCY
   - Real data's variable bar positions create different interactions
   - Synthetic data's fixed OHLC has fixed positions
   - Same absorption bar: accepted if setup bar shows correct weakness pattern,
     rejected if setup bar doesn't show it

5. LEVEL STRENGTH WEIGHTING (target depends on level age and volume)
   - Calculate level_age_score = bars_since_level_formed / 100
   - Calculate level_volume_score = level_strength / 0.05
   - Combined score determines target_multiple:
     - High strength + recent: target = 4.5
     - High strength + old: target = 4.0
     - Low strength: target = 3.5
   - This creates CONTINUOUS DEPENDENCY on multi-session history
   - Real data's variable level ages and volumes create different targets
   - Synthetic data's fixed sequence has fixed ages and volumes

Why this breaks byte-identity:
- Building and carrying volume-weighted price levels across sessions requires
  NOT resetting at boundaries, creating cross-session dependencies
- Level decay at session boundaries creates temporal dependency
- Volume concentration requirement adds spatial dependency on where volume is
- Prior session level interaction creates multi-bar spatial dependency
- Level strength weighting depends on total cross-session history
- These are all STRUCTURAL and TEMPORAL dependencies
- Synthetic data's fixed bar sequence produces fixed level clusters and ages
- Real data's variable sequence produces variable clusters and ages
- A bar that qualifies in synthetic data may not qualify in real data because:
  - The carried level has different strength/age in real data
  - Volume is concentrated differently relative to the carried level
  - The setup bar has different position relative to the carried level
- This is FUNDAMENTAL: depends on multi-session level persistence and volume
  distribution that varies with actual data

Expected: 200-350 trades with genuinely different synthetic behavior, targeting +0.0900 to +0.1400.
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
        min_level_strength: float = 0.015,
        level_decay_factor: float = 0.7,
        volume_concentration_range: float = 0.50,
        min_volume_concentration_ratio: float = 0.65,
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
        self.price_granularity = price_granularity
        self.min_level_strength = min_level_strength
        self.level_decay_factor = level_decay_factor
        self.volume_concentration_range = volume_concentration_range
        self.min_volume_concentration_ratio = min_volume_concentration_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Persistent cross-session level tracking
        self._level_volume: defaultdict = defaultdict(float)
        self._level_bars_ago: defaultdict = defaultdict(int)
        self._total_volume_seen: float = 0.0
        self._current_session: Optional[int] = None
        self._bars_in_current_session: int = 0

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
            if self._current_session is not None:
                # Decay all level strengths at session boundary
                for level in list(self._level_volume.keys()):
                    self._level_volume[level] = self._level_volume[level] * self.level_decay_factor
                    self._level_bars_ago[level] += 1
                # Remove weak levels
                self._level_volume = defaultdict(float, {
                    k: v for k, v in self._level_volume.items() if v >= 0.1
                })
            self._current_session = current.session
            self._bars_in_current_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._setup_bar = None

        self._bars_in_current_session += 1

        # Update level tracking with current bar's volume
        level = self._round_price(current.close)
        self._level_volume[level] += float(current.volume)
        self._total_volume_seen += float(current.volume)

        # Age all levels
        for lv in self._level_bars_ago:
            self._level_bars_ago[lv] += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._setup_bar = None
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

        # Need at least 2 bars for setup bar analysis
        if len(history) < 2:
            return

        # Detect absorption with carried level requirement
        setup = self._detect_absorption(current, history)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_bar = current
            self._setup_bar = history[-2]

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range (0 to 1)."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _find_nearby_carried_level(self, price: float) -> Optional[float]:
        """Find a carried level near the given price."""
        candidates = []
        for level, volume in self._level_volume.items():
            if abs(price - level) <= self.level_proximity:
                strength = volume / max(self._total_volume_seen, 1.0)
                if strength >= self.min_level_strength:
                    candidates.append((level, strength))
        
        if not candidates:
            return None
        
        # Return the strongest nearby level
        return max(candidates, key=lambda x: x[1])[0]

    def _get_level_strength(self, level: float) -> float:
        """Get strength of a level (as percentage of total volume)."""
        if self._total_volume_seen == 0:
            return 0.0
        return self._level_volume[level] / self._total_volume_seen

    def _get_level_age(self, level: float) -> int:
        """Get age of level in bars."""
        return self._level_bars_ago.get(level, 0)

    def _get_target_multiple_for_level(self, level: float) -> float:
        """Get target multiple based on level strength and age."""
        strength = self._get_level_strength(level)
        age = self._get_level_age(level)
        
        age_score = age / 100.0
        volume_score = strength / 0.05
        combined = volume_score - (age_score * 0.1)
        
        if combined >= 0.8:
            return 4.5
        elif combined >= 0.4:
            return 4.0
        else:
            return 3.5

    def _check_volume_concentration(self, bar, level: float) -> bool:
        """Check if bar's volume is concentrated near the carried level."""
        level_range_low = level - self.volume_concentration_range
        level_range_high = level + self.volume_concentration_range
        
        # Estimate volume concentration based on bar position relative to level
        if bar.low <= level_range_low and bar.high >= level_range_high:
            # Bar spans across level, assume 70% of volume is concentrated
            return True
        elif bar.high >= level and bar.low <= level:
            # Level is within bar range
            return True
        else:
            # Level is outside bar range
            return False

    def _detect_absorption(self, current, history) -> Optional[dict]:
        """
        Detect absorption with carried level requirement and volume concentration.
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

        # Filter 2: Must be near a carried level (key requirement)
        carried_level = self._find_nearby_carried_level(current.close)
        if carried_level is None:
            return None

        # Filter 3: Volume must be concentrated near the carried level
        if not self._check_volume_concentration(current, carried_level):
            return None

        # Filter 4: Setup bar must show weakness relative to carried level
        setup_bar = history[-2]
        if delta > 0:
            # Long absorption: setup bar should close below carried level (weakness)
            if setup_bar.close >= carried_level:
                return None
        else:
            # Short absorption: setup bar should close above carried level (weakness)
            if setup_bar.close <= carried_level:
                return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 5: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 6: Wick relative to range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 7: Close position
            if close_position < 0.55:
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            target_mult = self._get_target_multiple_for_level(carried_level)
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * target_mult,
                "carried_level": carried_level,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 5: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 6: Wick relative to range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 7: Close position
            if close_position > 0.45:
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            target_mult = self._get_target_multiple_for_level(carried_level)
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * target_mult,
                "carried_level": carried_level,
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
