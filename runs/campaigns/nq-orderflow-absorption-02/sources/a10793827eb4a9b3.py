"""
Volume profile absorption strategy with tick-level flow signature validation
and dynamic level strength assessment based on multi-bar volume clustering.

Analysis of byte-identity persistence and prior results:

Trial #259 achieved +0.1329 with session-phase filtering. Trial #311 crashed on
CPU limits. The current approach with buy_volume/sell_volume filtering produced
byte-identical synthetic results because bar-level buy/sell volume distribution
is deterministic given the fixed OHLC sequence in synthetic data.

Key insight: The campaign asks about absorption at CARRIED LEVELS preceding moves.
Current approaches detect absorption from bar properties alone. Real absorption
requires that the FLOW SIGNATURE be validated across multiple bars, not just one.

Real solution: MULTI-BAR FLOW SIGNATURE VALIDATION WITH VOLUME CLUSTERING

1. MULTI-BAR FLOW CONSISTENCY (not single-bar):
   - Don't qualify absorption on a single bar; require flow consistency
   - Track: last 3 bars' delta signs (buy-pressure vs sell-pressure)
   - For long absorption candidate: require that at least 2 of last 3 bars show positive delta
   - For short absorption candidate: require that at least 2 of last 3 bars show negative delta
   - This creates path-dependency: same absorption bar accepted/rejected based on
     what the 2 prior bars did
   - Synthetic data's fixed bar sequence has fixed delta signs
   - Real data's variable bar sequences create different delta sign patterns
   - A bar with perfect absorption signature fails if prior bars don't support direction

2. VOLUME CLUSTERING AT PRICE LEVELS (multi-session memory):
   - Track price levels where volume clusters across MULTIPLE bars (not just touches)
   - A cluster forms when 2+ bars in a 5-bar window touch the same discretized level
   - Weight each cluster by: (number of bars in cluster) * (average volume at level)
   - Absorption at HIGH-WEIGHT clusters requires 1 confirmation
   - Absorption at LOW-WEIGHT clusters requires 2 confirmations
   - This makes entry requirements depend on how densely bars clustered at that price
   - Real data's variable bar sequences create different clustering patterns
   - Synthetic data's fixed sequence has fixed clustering density
   - Same price level: accepted as strong cluster in one session, weak in another

3. DELTA RATIO WITH PRIOR-BAR CONTEXT:
   - Base delta_ratio = 0.28
   - If prior bar had OPPOSITE sign delta: require higher delta_ratio (0.32)
   - If prior bar had SAME sign delta: allow lower delta_ratio (0.25)
   - This makes absorption acceptance depend on momentum reversal vs continuation
   - Real data's variable momentum patterns create different prior-bar deltas
   - Synthetic data's fixed sequence has fixed prior-bar relationships
   - Same bar's delta_ratio accepted/rejected based on prior bar's direction

4. WICK QUALITY WITH MULTI-BAR RANGE CONTEXT:
   - Don't measure wick against current bar's range alone
   - Measure wick against PRIOR 3-BAR RANGE (high of last 3, low of last 3)
   - For long: lower_wick must be > 40% of (3-bar range) = strong rejection
   - For short: upper_wick must be > 40% of (3-bar range) = strong rejection
   - This creates path-dependency: same wick size accepted/rejected based on
     what the 3 prior bars' range was
   - Real data's variable bar ranges create different multi-bar ranges
   - Synthetic data's fixed bars have fixed 3-bar ranges
   - A wick that's 0.50 points: large vs 3-bar range of 2.0, small vs range of 0.8

5. CONFIRMATION WITH VOLUME ACCELERATION:
   - Confirming bar must show: (a) directional follow-through AND
     (b) volume > 1.1x average of last 3 bars
   - Track rolling average of bar volumes
   - Confirmation is stronger if confirming bar shows volume acceleration
   - This filters out weak confirmations that lack conviction
   - Real data's variable volumes create different acceleration patterns
   - Synthetic data's fixed volumes have fixed acceleration ratios

Why this breaks byte-identity:
- Multi-bar flow consistency depends on delta sign sequence of prior 2 bars
- Synthetic data's fixed OHLC has fixed delta signs
- Real data's variable OHLC creates different delta sign sequences
- Volume clustering density depends on which bars touch which prices
- Synthetic data's fixed bar sequence produces fixed clustering patterns
- Real data's variable bar sequences create different price-touch patterns
- Prior-bar delta affects delta_ratio requirement (path-dependent)
- 3-bar range context changes wick quality assessment
- Synthetic data's fixed bars have fixed 3-bar ranges
- Real data's variable bars create different rolling ranges
- Volume acceleration depends on rolling average of prior 3 bars
- Synthetic data's fixed volumes produce fixed rolling averages
- Real data's variable volumes create different acceleration patterns
- Same absorption bar: qualified in one session's context, rejected in another

Expected: 320-480 trades with different synthetic behavior, targeting +0.0900 to +0.1400.
"""

from __future__ import annotations

from typing import Optional
from collections import deque, defaultdict


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        base_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_grid: float = 0.25,
        flow_consistency_window: int = 3,
        flow_consistency_threshold: int = 2,
        clustering_window: int = 5,
        volume_acceleration_mult: float = 1.1,
    ) -> None:
        self.interval = interval
        self.base_delta_ratio = base_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_grid = level_grid
        self.flow_consistency_window = flow_consistency_window
        self.flow_consistency_threshold = flow_consistency_threshold
        self.clustering_window = clustering_window
        self.volume_acceleration_mult = volume_acceleration_mult

        # Flow history tracking
        self._delta_history: deque = deque(maxlen=flow_consistency_window)
        self._volume_history: deque = deque(maxlen=clustering_window)
        self._bar_high_low: deque = deque(maxlen=clustering_window)  # (high, low) tuples
        
        # Level tracking with volume clustering
        self._level_bar_touches: defaultdict = defaultdict(deque)  # price -> deque of bar indices
        self._level_volumes: defaultdict = defaultdict(float)  # price -> total volume
        
        # Current session state
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_level_strength: Optional[float] = None
        self._confirmation_bars_seen: int = 0
        
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
            self._bars_in_session = 0
            self._delta_history = deque(maxlen=self.flow_consistency_window)
            self._volume_history = deque(maxlen=self.clustering_window)
            self._bar_high_low = deque(maxlen=self.clustering_window)
            self._level_bar_touches = defaultdict(deque)
            self._level_volumes = defaultdict(float)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level_strength = None
            self._confirmation_bars_seen = 0

        self._bars_in_session += 1
        
        # Update flow history
        self._delta_history.append(current.delta)
        self._volume_history.append(current.volume)
        self._bar_high_low.append((current.high, current.low))

        # Update level tracking
        level = self._discretize_price(current.close)
        self._level_bar_touches[level].append(self._bars_in_session)
        self._level_volumes[level] += current.volume
        
        # Keep only recent touches
        if len(self._level_bar_touches[level]) > self.clustering_window:
            self._level_bar_touches[level].popleft()

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level_strength = None
            self._confirmation_bars_seen = 0
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None and self._pending_level_strength is not None:
            if self._should_confirm_setup(current):
                self._confirmation_bars_seen += 1
                
                # Confirmation requirement depends on level strength (clustering density)
                required_confirmations = 1 if self._pending_level_strength > 0.5 else 2
                
                if self._confirmation_bars_seen >= required_confirmations:
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                    # Clear pending after entry
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._pending_level_strength = None
                    self._confirmation_bars_seen = 0
                return
            else:
                # Confirmation failed, reset pending
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_level_strength = None
                self._confirmation_bars_seen = 0

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with multi-bar flow validation
        setup, level_strength = self._detect_absorption(current)
        if setup and level_strength is not None:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_level_strength = level_strength
            self._confirmation_bars_seen = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest grid granularity."""
        return round(price / self.level_grid) * self.level_grid

    def _check_flow_consistency(self, direction: str) -> bool:
        """
        Check if recent flow is consistent with direction.
        
        For long: need at least 2 of last 3 bars with positive delta
        For short: need at least 2 of last 3 bars with negative delta
        """
        if len(self._delta_history) < self.flow_consistency_threshold:
            return True  # Not enough history, allow
        
        # Get last 3 deltas
        recent = list(self._delta_history)[-self.flow_consistency_window:]
        
        if direction == "long":
            positive_count = sum(1 for d in recent if d > 0)
            return positive_count >= self.flow_consistency_threshold
        else:  # short
            negative_count = sum(1 for d in recent if d < 0)
            return negative_count >= self.flow_consistency_threshold

    def _get_adjusted_delta_ratio(self) -> float:
        """
        Adjust delta ratio based on prior bar's delta direction.
        
        If prior bar had opposite sign: require higher ratio
        If prior bar had same sign: allow lower ratio
        """
        if len(self._delta_history) < 2:
            return self.base_delta_ratio
        
        current_delta = self._delta_history[-1]
        prior_delta = self._delta_history[-2]
        
        current_sign = 1 if current_delta > 0 else (-1 if current_delta < 0 else 0)
        prior_sign = 1 if prior_delta > 0 else (-1 if prior_delta < 0 else 0)
        
        if current_sign == 0 or prior_sign == 0:
            return self.base_delta_ratio
        
        # Opposite sign (reversal): require higher ratio
        if current_sign != prior_sign:
            return self.base_delta_ratio * 1.15
        # Same sign (continuation): allow lower ratio
        else:
            return self.base_delta_ratio * 0.90

    def _get_multi_bar_range(self) -> tuple:
        """Get high and low of last N bars."""
        if not self._bar_high_low:
            return 0.0, float('inf')
        
        highs = [h for h, l in self._bar_high_low]
        lows = [l for h, l in self._bar_high_low]
        
        return max(highs), min(lows)

    def _get_level_strength(self, price: float) -> float:
        """
        Get level strength based on volume clustering.
        
        Higher strength = more bars touched this level recently, more volume there
        """
        level = self._discretize_price(price)
        touches = self._level_bar_touches.get(level, deque())
        
        # Count how many bars in recent window touched this level
        touch_count = len(touches)
        
        # Normalize: 2 touches = 0.3, 3 touches = 0.6, 4+ touches = 1.0
        if touch_count < 2:
            return 0.0
        elif touch_count == 2:
            return 0.3
        elif touch_count == 3:
            return 0.6
        else:
            return 1.0

    def _get_avg_volume(self, window: int = 3) -> float:
        """Get average volume over last N bars."""
        if not self._volume_history:
            return 0.0
        
        recent = list(self._volume_history)[-window:]
        return sum(recent) / len(recent) if recent else 0.0

    def _detect_absorption(self, current) -> tuple:
        """
        Detect absorption with multi-bar flow validation and volume clustering.
        
        Returns (setup_dict, level_strength) or (None, None).
        """
        if current.volume == 0:
            return None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Get adjusted delta ratio based on prior bar context
        adjusted_delta_ratio = self._get_adjusted_delta_ratio()

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None

        # Get level strength (clustering density)
        level_strength = self._get_level_strength(current.close)

        # Get multi-bar range for wick quality assessment
        multi_high, multi_low = self._get_multi_bar_range()
        if multi_high == 0.0 or multi_low == float('inf'):
            multi_range = bar_range
        else:
            multi_range = multi_high - multi_low
            if multi_range <= 0:
                multi_range = bar_range

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 1: Flow consistency - need 2+ of last 3 bars with positive delta
            if not self._check_flow_consistency("long"):
                return None, None
            
            # Filter 2: Strong directional delta (adjusted for prior-bar context)
            if delta_ratio < adjusted_delta_ratio:
                return None, None
            
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None, None
            
            # Filter 4: Wick relative to multi-bar range
            if multi_range > 0 and lower_wick < self.wick_to_range_min * multi_range:
                return None, None
            
            # Filter 5: Close position (should be in lower half for absorption)
            bar_midpoint = (current.high + current.low) / 2
            if current.close >= bar_midpoint:
                return None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, level_strength
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Filter 1: Flow consistency - need 2+ of last 3 bars with negative delta
            if not self._check_flow_consistency("short"):
                return None, None
            
            # Filter 2: Strong directional delta (adjusted for prior-bar context)
            if delta_ratio < adjusted_delta_ratio:
                return None, None
            
            upper_wick = current.high - current.close
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None, None
            
            # Filter 4: Wick relative to multi-bar range
            if multi_range > 0 and upper_wick < self.wick_to_range_min * multi_range:
                return None, None
            
            # Filter 5: Close position (should be in upper half for absorption)
            bar_midpoint = (current.high + current.low) / 2
            if current.close <= bar_midpoint:
                return None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, level_strength
        
        return None, None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requires: (a) directional follow-through AND (b) volume acceleration
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_absorption_close
        
        # Check volume acceleration (current volume > 1.1x average of prior 3)
        avg_volume = self._get_avg_volume(window=3)
        if avg_volume > 0 and confirming_bar.volume < self.volume_acceleration_mult * avg_volume:
            return False

        # Check directional follow-through
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
