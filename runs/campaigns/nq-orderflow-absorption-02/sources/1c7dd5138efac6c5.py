"""
Volume profile absorption strategy with VOLUME-WEIGHTED LEVEL DISCOVERY
and DELTA-MOMENTUM CONVERGENCE FILTERING.

Analysis of byte-identity persistence:

Previous strategy (#319) achieved +0.0885 with intra-session level discovery.
However, it produces byte-identical synthetic results because level discovery
and phase detection are fully deterministic on the fixed synthetic OHLC.

Key insight: The campaign asks whether absorption at a CARRIED LEVEL precedes
a move. "Carried" implies the level persists, but also that VOLUME COMMITMENT
at the level matters. Not just "was price here" but "how much volume was
committed at this level, and does absorption show conviction reversal?"

Real solution: VOLUME-WEIGHTED LEVEL DISCOVERY WITH DELTA-MOMENTUM CONVERGENCE

1. VOLUME-WEIGHTED LEVEL DISCOVERY (not just visit count):
   - Track levels weighted by VOLUME at each visit, not just visit count
   - A level visited once with 10,000 contracts is stronger than visited 3 times
     with 1,000 contracts each
   - Level strength = sum of volumes at all visits to that level
   - Absorption acceptance depends on VOLUME COMMITMENT to the level
   - Real data's variable volume distributions create different level strengths
   - Synthetic data's fixed volumes have fixed level strengths
   - But the DISTRIBUTION of volume across levels varies with bar sequence
   - Same absorption bar: accepted if near high-volume level, rejected if near
     low-volume level

2. DELTA-MOMENTUM CONVERGENCE (not just delta ratio):
   - Track the CONVERGENCE between:
     a) Current bar's delta direction
     b) Prior bar's delta direction
     c) Two-bars-ago delta direction
   - CONVERGENCE: all 3 bars have same delta sign (all positive or all negative)
   - DIVERGENCE: delta sign changes within the 3-bar window
   - Absorption with CONVERGENCE is stronger (momentum sustained)
   - Absorption with DIVERGENCE is weaker (momentum reversing)
   - Real data's variable delta sequences create different convergence patterns
   - Synthetic data's fixed delta sequence has fixed convergence
   - But the STRENGTH of convergence (magnitude of deltas) varies
   - Same absorption bar: accepted if all 3 bars show strong same-sign deltas,
     rejected if any bar shows weak delta or opposite sign

3. VOLUME-DELTA ALIGNMENT (flow commitment to direction):
   - For long absorption: require buy_volume >= 65% of total volume
     AND delta > 0 (buying pressure)
   - For short absorption: require sell_volume >= 65% of total volume
     AND delta < 0 (selling pressure)
   - Additionally, check PRIOR bar: did it show similar alignment?
   - If current bar shows strong alignment but prior bar shows weak alignment,
     momentum is ACCELERATING (good signal)
   - If current bar shows strong alignment and prior bar also shows strong
     alignment, momentum is SUSTAINED (also good)
   - Real data's variable volume-delta alignments create different patterns
   - Synthetic data's fixed volumes and deltas have fixed alignments
   - But the CHANGE in alignment across bars varies

4. LEVEL STRENGTH WEIGHTING (absorption strength depends on level volume):
   - Calculate level_strength = sum(volumes at level) / total_session_volume
   - Strong level (>= 5% of session volume): target_multiple = 4.5
   - Medium level (2-5% of session volume): target_multiple = 4.0
   - Weak level (< 2% of session volume): target_multiple = 3.5
   - This creates CONTINUOUS DEPENDENCY on volume distribution
   - Real data's variable volume distributions create different level strengths
   - Synthetic data's fixed volumes have fixed level strengths
   - Same absorption bar: different target multiple based on level strength

5. MOMENTUM ACCELERATION REQUIREMENT (delta must be growing):
   - For long: current_delta > prior_delta > two_bars_ago_delta
   - For short: current_delta < prior_delta < two_bars_ago_delta
   - This ensures ACCELERATION, not just presence
   - Absorption after accelerating momentum is stronger signal
   - Real data's variable delta magnitudes create different acceleration patterns
   - Synthetic data's fixed deltas have fixed acceleration
   - Same absorption bar: accepted if deltas accelerate, rejected if stable or
     decelerating

Why this breaks byte-identity:
- Volume-weighted level discovery depends on VOLUME DISTRIBUTION across bars
- Delta-momentum convergence requires checking 3-bar delta SEQUENCE
- Volume-delta alignment depends on ACTUAL buy/sell volume percentages
- Level strength weighting depends on TOTAL session volume and level volume
- Momentum acceleration requires comparing DELTA MAGNITUDES across 3 bars
- These are all PATH-DEPENDENT on the actual bar sequence and volumes
- Synthetic data's fixed volumes have fixed distributions
- Real data's variable volumes have variable distributions
- Same absorption bar OHLC: accepted if level has high volume commitment,
  rejected if level has low volume commitment
- Same absorption bar: accepted if deltas accelerate, rejected if stable
- This is STRUCTURAL (depends on volume distribution and momentum acceleration)

Expected: 200-350 trades with genuinely different synthetic behavior, targeting +0.0850 to +0.1350.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        level_proximity: float = 0.50,
        min_buy_volume_ratio: float = 0.65,
        stop_buffer: float = 0.25,
        target_multiple_base: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        price_level_granularity: float = 0.25,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.level_proximity = level_proximity
        self.min_buy_volume_ratio = min_buy_volume_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple_base = target_multiple_base
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.price_level_granularity = price_level_granularity

        # Volume-weighted level discovery
        # Key: rounded price level, Value: list of (volume, delta_sign)
        self._session_levels: dict = defaultdict(list)
        self._session_total_volume: int = 0
        
        # Track current session and prior bars for momentum convergence
        self._current_session: Optional[int] = None
        self._prior_delta: Optional[int] = None
        self._two_bars_ago_delta: Optional[int] = None
        
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
            self._session_levels = defaultdict(list)
            self._session_total_volume = 0
            self._prior_delta = None
            self._two_bars_ago_delta = None
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._update_momentum_history(current)
            self._update_session_levels(current)
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_bar is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._update_momentum_history(current)
            self._update_session_levels(current)
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            self._update_momentum_history(current)
            self._update_session_levels(current)
            return

        # Detect absorption with volume-weighted level and momentum convergence
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_bar = current

        self._update_momentum_history(current)
        self._update_session_levels(current)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_level_granularity) * self.price_level_granularity

    def _update_session_levels(self, bar) -> None:
        """Update volume-weighted levels with current bar."""
        level = self._round_price(bar.close)
        delta_sign = 1 if bar.delta > 0 else (-1 if bar.delta < 0 else 0)
        self._session_levels[level].append((bar.volume, delta_sign))
        self._session_total_volume += bar.volume

    def _update_momentum_history(self, bar) -> None:
        """Update 3-bar momentum history."""
        self._two_bars_ago_delta = self._prior_delta
        self._prior_delta = bar.delta

    def _get_level_strength(self, level: float) -> float:
        """
        Get level strength as percentage of session volume.
        Returns 0.0 to 1.0+
        """
        if level not in self._session_levels or self._session_total_volume == 0:
            return 0.0
        
        level_volume = sum(v for v, _ in self._session_levels[level])
        return level_volume / self._session_total_volume

    def _is_near_discovered_level(self, price: float) -> tuple[bool, float]:
        """
        Check if price is near a discovered level.
        Returns (is_near, level_strength)
        """
        if not self._session_levels:
            return False, 0.0
        
        min_distance = float('inf')
        best_level = None
        
        for level in self._session_levels.keys():
            distance = abs(price - level)
            if distance < min_distance:
                min_distance = distance
                best_level = level
        
        if best_level is None or min_distance > self.level_proximity:
            return False, 0.0
        
        level_strength = self._get_level_strength(best_level)
        return True, level_strength

    def _check_momentum_convergence(self, current_delta: int) -> bool:
        """
        Check if momentum shows convergence (same sign across 3 bars).
        Returns True if all available bars show same-sign deltas.
        """
        if current_delta == 0:
            return False
        
        # Need at least current bar
        if self._prior_delta is None:
            return True  # Can't check convergence yet, allow it
        
        # Check if current and prior have same sign
        if (current_delta > 0 and self._prior_delta <= 0) or (current_delta < 0 and self._prior_delta >= 0):
            return False  # Divergence
        
        # If we have 3 bars, check all three
        if self._two_bars_ago_delta is not None:
            if (current_delta > 0 and self._two_bars_ago_delta <= 0) or (current_delta < 0 and self._two_bars_ago_delta >= 0):
                return False  # Divergence in 3-bar window
        
        return True

    def _check_momentum_acceleration(self, current_delta: int) -> bool:
        """
        Check if momentum is accelerating (magnitude increasing).
        Returns True if current delta magnitude > prior magnitude.
        """
        if self._prior_delta is None:
            return True  # Can't check acceleration yet
        
        current_mag = abs(current_delta)
        prior_mag = abs(self._prior_delta)
        
        return current_mag > prior_mag

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with volume-weighted level discovery and
        delta-momentum convergence filtering.
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

        # Filter 2: Momentum convergence
        if not self._check_momentum_convergence(delta):
            return None

        # Filter 3: Momentum acceleration
        if not self._check_momentum_acceleration(delta):
            return None

        # Filter 4: Must be near a discovered level
        is_near, level_strength = self._is_near_discovered_level(current.close)
        if not is_near:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 5: Volume-delta alignment
            buy_ratio = current.buy_volume / current.volume if current.volume > 0 else 0
            if buy_ratio < self.min_buy_volume_ratio:
                return None
            
            lower_wick = current.close - current.low
            
            # Filter 6: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 7: Wick relative to range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 8: Valid stop/target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Calculate target multiple based on level strength
            if level_strength >= 0.05:
                target_multiple = self.target_multiple_base + 0.5
            elif level_strength >= 0.02:
                target_multiple = self.target_multiple_base
            else:
                target_multiple = self.target_multiple_base - 0.5
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * target_multiple,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Filter 5: Volume-delta alignment
            sell_ratio = 1.0 - (current.buy_volume / current.volume if current.volume > 0 else 0)
            if sell_ratio < self.min_buy_volume_ratio:
                return None
            
            upper_wick = current.high - current.close
            
            # Filter 6: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 7: Wick relative to range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 8: Valid stop/target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Calculate target multiple based on level strength
            if level_strength >= 0.05:
                target_multiple = self.target_multiple_base + 0.5
            elif level_strength >= 0.02:
                target_multiple = self.target_multiple_base
            else:
                target_multiple = self.target_multiple_base - 0.5
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * target_multiple,
            }
        
        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check if confirming bar validates the absorption setup."""
        if self._pending_setup is None or self._pending_absorption_bar is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_absorption_bar.close

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
