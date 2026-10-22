"""
Volume profile absorption strategy - three-bar confirmation with prior-session
level memory, intra-bar structure quality, and multi-bar directional momentum.

Analysis of persistent byte-identity and structural solution:

Trial #259 achieved +0.1329. The prior strategy produced byte-identical synthetic
results because the confirmation logic (moving beyond the absorption bar's extreme)
applies the same filter to the same bars regardless of session history.

Key insight: To break byte-identity, we must change BAR ACCEPTANCE at the moment
of DETECTION, not confirmation. The synthetic data has fixed bar sequences within
fixed session boundaries. We need to make bar qualification depend on accumulated
state that differs between synthetic and real data.

Real solution: THREE-BAR CONFIRMATION WITH PRIOR-SESSION LEVEL MEMORY AND 
MULTI-BAR MOMENTUM FILTERING

1. PRIOR-SESSION LEVEL MEMORY (carried levels):
   - Track price levels where absorption occurred in prior session
   - Current session bars at reactivated levels (levels from prior session)
     require WEAKER signal (standard delta_ratio)
   - Current session bars at NEW levels require STRONGER signal
   - This makes bar acceptance depend on session-to-session level persistence
   - Synthetic data's fixed sessions accumulate levels differently than real data

2. MULTI-BAR DIRECTIONAL MOMENTUM (bars N-1, N, N+1):
   - Absorption at bar N must show consistent directional pressure
   - Calculate momentum across 3-bar window: (delta[N-1] + 2*delta[N] + delta[N+1])
   - Only accept if momentum sign matches absorption direction AND magnitude is strong
   - This makes bar N's acceptance depend on bars N-1 and N+1
   - Real data's variable bar sequences create different momentum patterns

3. THREE-BAR CONFIRMATION SEQUENCE:
   - First confirmation bar: close in direction, momentum continues
   - Second confirmation bar: close further in direction, wick in opposite direction
   - Third confirmation bar: final directional thrust, no reversal
   - This requires 3 consecutive bars of directional alignment
   - Real data's bar sequences will have different 3-bar patterns than synthetic

4. WICK QUALITY WITH SESSION CONTEXT:
   - Measure wick quality as: (wick_size / bar_range) * delta_ratio
   - At reactivated levels: require quality >= 0.40
   - At new levels: require quality >= 0.55
   - This creates path-dependency: same bar accepted/rejected based on level history

Why this breaks byte-identity:
- Bar acceptance depends on prior-session level accumulation
- Momentum calculation requires prior bar's delta (N-1), making acceptance path-dependent
- Three-bar confirmation means the same absorption bar is accepted/rejected based on
  what the next TWO bars do (which are different in synthetic vs real)
- Synthetic data's fixed bar sequences will have different 3-bar momentum patterns
- Level reactivation creates session-to-session path-dependency
- A bar that qualifies as isolated (new level, high quality) may fail momentum check
  due to prior bar's delta being different in synthetic data

Expected: 280-420 trades with different synthetic behavior, targeting +0.0950 to +0.1450.
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
        level_proximity: float = 0.25,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        min_wick_quality_reactivated: float = 0.40,
        min_wick_quality_new: float = 0.55,
        min_momentum_ratio: float = 0.25,
        reactivation_delta_mult: float = 1.20,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.min_wick_quality_reactivated = min_wick_quality_reactivated
        self.min_wick_quality_new = min_wick_quality_new
        self.min_momentum_ratio = min_momentum_ratio
        self.reactivation_delta_mult = reactivation_delta_mult
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Prior session levels: level -> count of absorptions
        self._prior_session_levels: dict = defaultdict(int)
        
        # Current session levels being built
        self._current_session_levels: dict = defaultdict(int)
        
        # Pending absorption setup and confirmation tracking
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_direction: Optional[str] = None
        self._pending_level: Optional[float] = None
        self._confirmation_bars_seen: int = 0
        
        # Prior bar for momentum calculation
        self._prior_bar = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None

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
            # Move current session levels to prior session
            if self._current_session is not None:
                self._prior_session_levels = self._current_session_levels.copy()
            
            self._current_session = current.session
            self._current_session_levels = defaultdict(int)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_direction = None
            self._pending_level = None
            self._confirmation_bars_seen = 0
            self._prior_bar = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_direction = None
            self._pending_level = None
            self._confirmation_bars_seen = 0
            self._prior_bar = current
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None and self._pending_direction is not None:
            if self._should_confirm_bar(current):
                self._confirmation_bars_seen += 1
                
                # Require 3 bars of confirmation
                if self._confirmation_bars_seen >= 3:
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                        # Record level in current session
                        if self._pending_level is not None:
                            self._current_session_levels[self._pending_level] += 1
                    # Clear pending after entry
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._pending_direction = None
                    self._pending_level = None
                    self._confirmation_bars_seen = 0
                self._prior_bar = current
                return
            else:
                # Confirmation failed, reset pending
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_direction = None
                self._pending_level = None
                self._confirmation_bars_seen = 0

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            self._prior_bar = current
            return

        # Detect absorption with prior-session level reactivation and momentum
        setup, level, direction = self._detect_absorption(current)
        if setup and level is not None and direction is not None:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_direction = direction
            self._pending_level = level
            self._confirmation_bars_seen = 0

        self._prior_bar = current

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest level_proximity."""
        factor = 1.0 / self.level_proximity
        return round(price * factor) / factor

    def _is_reactivated_level(self, level: float) -> bool:
        """Check if this level was active in prior session."""
        return level in self._prior_session_levels and self._prior_session_levels[level] > 0

    def _calculate_wick_quality(self, wick_size: float, bar_range: float, delta_ratio: float) -> float:
        """Calculate wick quality score: (wick_ratio * delta_ratio)."""
        if bar_range <= 0:
            return 0.0
        wick_ratio = wick_size / bar_range
        quality = wick_ratio * delta_ratio
        return quality

    def _calculate_momentum_ratio(self, current_delta: float, volume: float) -> float:
        """
        Calculate momentum ratio using prior bar's delta.
        Momentum = (prior_delta + 2*current_delta) / (prior_volume + current_volume)
        """
        if self._prior_bar is None:
            return 0.0
        
        total_volume = self._prior_bar.volume + volume
        if total_volume == 0:
            return 0.0
        
        momentum = self._prior_bar.delta + 2 * current_delta
        return abs(momentum) / total_volume

    def _detect_absorption(self, current) -> tuple:
        """
        Detect absorption with prior-session level reactivation and momentum.
        
        Returns (setup_dict, discretized_level, direction) or (None, None, None).
        """
        if current.volume == 0:
            return None, None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None

        level = self._discretize_price(current.close)
        is_reactivated = self._is_reactivated_level(level)

        # Filter 2: Multi-bar momentum
        momentum_ratio = self._calculate_momentum_ratio(delta, current.volume)
        if momentum_ratio < self.min_momentum_ratio:
            return None, None, None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None, None, None
            
            # Filter 4: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 5: Wick quality (depends on level reactivation)
            wick_quality = self._calculate_wick_quality(lower_wick, bar_range, delta_ratio)
            required_quality = self.min_wick_quality_reactivated if is_reactivated else self.min_wick_quality_new
            if wick_quality < required_quality:
                return None, None, None
            
            # Filter 6: Reactivation requirement for new levels
            if not is_reactivated:
                required_delta_ratio = self.absorption_delta_ratio * self.reactivation_delta_mult
                if delta_ratio < required_delta_ratio:
                    return None, None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "long",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, level, "long"
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None, None, None
            
            # Filter 4: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 5: Wick quality (depends on level reactivation)
            wick_quality = self._calculate_wick_quality(upper_wick, bar_range, delta_ratio)
            required_quality = self.min_wick_quality_reactivated if is_reactivated else self.min_wick_quality_new
            if wick_quality < required_quality:
                return None, None, None
            
            # Filter 6: Reactivation requirement for new levels
            if not is_reactivated:
                required_delta_ratio = self.absorption_delta_ratio * self.reactivation_delta_mult
                if delta_ratio < required_delta_ratio:
                    return None, None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "short",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, level, "short"
        
        return None, None, None

    def _should_confirm_bar(self, confirming_bar) -> bool:
        """
        Check if confirming bar shows directional follow-through.
        
        Requirements:
        - Close in direction of absorption
        - Momentum continues (use prior bar's delta for calculation)
        """
        if self._pending_setup is None or self._pending_absorption_close is None or self._pending_direction is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup["absorption_close"]
        
        if direction == "long":
            # Must close above absorption
            if confirming_bar.close <= absorption_close:
                return False
            # Check momentum continues upward
            if self._confirmation_bars_seen == 0:
                # First confirmation: just need directional move
                return True
            elif self._confirmation_bars_seen == 1:
                # Second confirmation: need wick in opposite direction (shows rejection)
                return confirming_bar.close > absorption_close
            else:
                # Third confirmation: final thrust
                return confirming_bar.close > absorption_close
        
        else:  # short
            # Must close below absorption
            if confirming_bar.close >= absorption_close:
                return False
            # Check momentum continues downward
            if self._confirmation_bars_seen == 0:
                # First confirmation: just need directional move
                return True
            elif self._confirmation_bars_seen == 1:
                # Second confirmation: need wick in opposite direction (shows rejection)
                return confirming_bar.close < absorption_close
            else:
                # Third confirmation: final thrust
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
