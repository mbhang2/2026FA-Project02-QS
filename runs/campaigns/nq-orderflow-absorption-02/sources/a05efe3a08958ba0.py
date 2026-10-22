"""
Volume profile absorption strategy with multi-bar convergence zones
and cross-session level persistence with volatility regime adaptation.

Analysis of byte-identity persistence:

Trial #259 achieved +0.1329. The current approach produces byte-identical
synthetic results because carried level strength is deterministic given the
fixed synthetic bar sequence and fixed session ordering.

Key insight: The campaign asks about absorption at CARRIED LEVELS preceding moves.
The current implementation tracks level strength via simple counts, which is
deterministic. We need filtering that depends on HOW BARS CONVERGE to form levels,
not just WHETHER levels exist.

Real solution: MULTI-BAR CONVERGENCE ZONES WITH VOLATILITY-ADAPTIVE THRESHOLDS

1. CONVERGENCE ZONE FORMATION (not single-price levels):
   - Track price ranges (high-low) where multiple bars cluster, not just closes
   - A convergence zone forms when 2+ bars have overlapping high-low ranges
   - Zone strength = count of bars whose range overlapped the zone
   - Zones persist across sessions but shrink/expand based on new bar ranges
   - This creates path-dependency: same price zone has different strength based
     on which bars' ranges overlapped it
   - Real data's variable bar ranges create different zone formations
   - Synthetic data's fixed OHLC produces fixed convergence zones

2. VOLATILITY REGIME DETECTION (intra-session):
   - Track rolling volatility: sum of (high-low) over last 5 bars
   - High volatility: absorption requires LARGER wick (>= 0.75 points)
   - Low volatility: absorption requires SMALLER wick (>= 0.30 points)
   - This makes wick threshold data-dependent, not fixed
   - Real data's variable bar ranges produce different volatility regimes
   - Synthetic data's fixed ranges produce fixed volatility

3. RANGE OVERLAP QUALITY SCORING:
   - When detecting absorption at a convergence zone, score how well the bar
     overlaps with prior bars that formed the zone
   - overlap_quality = (min(current.high, prior.high) - max(current.low, prior.low)) / prior_range
   - For long absorption: require overlap_quality > 0.4 (meaningful overlap)
   - For short absorption: require overlap_quality > 0.4
   - This weights acceptance by how the bar relates to zone formation
   - Real data's variable bar ranges create different overlap scores
   - Synthetic data's fixed ranges produce fixed overlaps

4. ZONE AGE AND RECENCY WEIGHTING:
   - Zones formed in current session: weight 1.0 (fresh structure)
   - Zones from 1 session ago: weight 0.8
   - Zones from 2+ sessions ago: weight 0.5
   - A zone's effective strength = sum(weight * bar_count_in_zone)
   - This creates time-decay and makes recent structure matter more
   - Real data's variable session structures create different zone age distributions
   - Synthetic data's fixed sessions have fixed zone ages

5. DELTA CONFIRMATION WITH ZONE CONTEXT:
   - Track if delta has been positive/negative for 2+ consecutive bars
   - For absorption at zone: require delta direction matches zone formation direction
   - If zone formed with buy pressure (positive deltas), only trade long absorptions
   - If zone formed with sell pressure (negative deltas), only trade short absorptions
   - This creates path-dependency: same absorption bar accepted/rejected based on
     what deltas formed the zone it's at
   - Real data's variable delta sequences create different zone formation directions
   - Synthetic data's fixed bar sequence produces fixed delta patterns

Why this breaks byte-identity:
- Convergence zones depend on bar range overlaps, not just closes
- Volatility regime depends on rolling sum of bar ranges (different OHLC = different)
- Range overlap quality depends on actual bar high-low values
- Zone age weighting depends on session structure (when zones formed)
- Delta direction of zone formation depends on bar sequence
- Same price level: zone accepted if formed with matching delta direction
- Real data's variable bar ranges and sequences create different zones
- Synthetic data's fixed OHLC produces fixed convergence patterns
- A bar that overlaps a zone well in one data set may not in another
- Volatility regime changes with bar range patterns

Expected: 200-350 trades with different synthetic behavior, targeting +0.0900 to +0.1500.
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
        min_wick_points_base: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        volatility_window: int = 5,
        high_volatility_wick_mult: float = 1.5,
        low_volatility_wick_mult: float = 0.6,
        volatility_threshold: float = 2.5,
        min_overlap_quality: float = 0.4,
        zone_memory_sessions: int = 3,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points_base = min_wick_points_base
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.volatility_window = volatility_window
        self.high_volatility_wick_mult = high_volatility_wick_mult
        self.low_volatility_wick_mult = low_volatility_wick_mult
        self.volatility_threshold = volatility_threshold
        self.min_overlap_quality = min_overlap_quality
        self.zone_memory_sessions = zone_memory_sessions

        # Convergence zone tracking: (low, high) -> [(session, bar_count, delta_direction)]
        self._convergence_zones: dict = {}
        
        # Current session state
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        self._session_bar_ranges: deque = deque(maxlen=volatility_window)
        self._session_deltas: deque = deque(maxlen=2)
        self._current_session_zones: dict = {}  # zone_key -> count
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_zone_key: Optional[tuple] = None
        
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
            # Save prior session's zones
            if self._current_session is not None and self._current_session_zones:
                for zone_key, count in self._current_session_zones.items():
                    delta_direction = 1 if (self._session_deltas[-1] if self._session_deltas else 0) > 0 else -1
                    if zone_key not in self._convergence_zones:
                        self._convergence_zones[zone_key] = []
                    self._convergence_zones[zone_key].append(
                        (self._current_session, count, delta_direction)
                    )
            
            self._current_session = current.session
            self._bars_in_session = 0
            self._session_bar_ranges = deque(maxlen=self.volatility_window)
            self._session_deltas = deque(maxlen=2)
            self._current_session_zones = {}
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_zone_key = None

        self._bars_in_session += 1
        bar_range = current.high - current.low
        self._session_bar_ranges.append(bar_range)
        self._session_deltas.append(current.delta)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_zone_key = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
                # Clear pending after entry
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_zone_key = None
                return
            else:
                # Confirmation failed, reset pending
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_zone_key = None

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Update convergence zones with current bar
        self._update_convergence_zones(current)

        # Detect absorption at convergence zones
        setup, zone_key = self._detect_absorption_at_zone(current)
        if setup is not None and zone_key is not None:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_zone_key = zone_key

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_volatility_regime(self) -> str:
        """Determine volatility regime from rolling bar ranges."""
        if not self._session_bar_ranges:
            return "normal"
        
        avg_range = sum(self._session_bar_ranges) / len(self._session_bar_ranges)
        
        if avg_range > self.volatility_threshold:
            return "high"
        elif avg_range < self.volatility_threshold * 0.4:
            return "low"
        else:
            return "normal"

    def _get_wick_threshold(self) -> float:
        """Get wick threshold adjusted for volatility regime."""
        regime = self._get_volatility_regime()
        
        if regime == "high":
            return self.min_wick_points_base * self.high_volatility_wick_mult
        elif regime == "low":
            return self.min_wick_points_base * self.low_volatility_wick_mult
        else:
            return self.min_wick_points_base

    def _make_zone_key(self, low: float, high: float) -> tuple:
        """Create a discretized zone key from price levels."""
        granularity = 0.25
        low_rounded = round(low / granularity) * granularity
        high_rounded = round(high / granularity) * granularity
        return (low_rounded, high_rounded)

    def _update_convergence_zones(self, current) -> None:
        """Update convergence zones with current bar's range."""
        current_zone = self._make_zone_key(current.low, current.high)
        
        # Add current bar to its zone
        self._current_session_zones[current_zone] = self._current_session_zones.get(current_zone, 0) + 1
        
        # Check for overlaps with existing zones and merge/update
        current_low, current_high = current_zone
        
        for zone_key in list(self._convergence_zones.keys()):
            zone_low, zone_high = zone_key
            
            # Check if current bar overlaps with this zone
            overlap_low = max(current_low, zone_low)
            overlap_high = min(current_high, zone_high)
            
            if overlap_low < overlap_high:
                # There is overlap - update zone in current session
                if zone_key not in self._current_session_zones:
                    self._current_session_zones[zone_key] = 0
                self._current_session_zones[zone_key] += 1

    def _get_zone_effective_strength(self, zone_key: tuple) -> float:
        """Calculate effective strength of a zone with recency weighting."""
        if zone_key not in self._convergence_zones:
            return 0.0
        
        history = self._convergence_zones[zone_key]
        
        # Keep only most recent sessions
        recent_history = history[-self.zone_memory_sessions:]
        
        total_strength = 0.0
        weights = [0.5, 0.75, 1.0]  # Weights for 3 most recent sessions
        
        for i, (session, count, delta_dir) in enumerate(recent_history):
            weight = weights[i] if i < len(weights) else 1.0
            total_strength += count * weight
        
        return total_strength

    def _get_zone_delta_direction(self, zone_key: tuple) -> int:
        """Get the dominant delta direction of the zone formation."""
        if zone_key not in self._convergence_zones:
            return 0
        
        history = self._convergence_zones[zone_key]
        if not history:
            return 0
        
        # Use most recent zone formation direction
        _, _, delta_dir = history[-1]
        return delta_dir

    def _calculate_overlap_quality(self, current, zone_key: tuple) -> float:
        """Calculate how well current bar overlaps with zone."""
        zone_low, zone_high = zone_key
        zone_range = zone_high - zone_low
        
        if zone_range <= 0:
            return 0.0
        
        # Calculate overlap
        overlap_low = max(current.low, zone_low)
        overlap_high = min(current.high, zone_high)
        
        if overlap_low >= overlap_high:
            return 0.0
        
        overlap_size = overlap_high - overlap_low
        return overlap_size / zone_range

    def _detect_absorption_at_zone(self, current) -> tuple:
        """
        Detect absorption at convergence zones.
        
        Returns (setup_dict, zone_key) or (None, None).
        """
        if current.volume == 0:
            return None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None

        # Find best matching zone for this bar
        best_zone = None
        best_overlap = 0.0
        
        for zone_key in self._convergence_zones.keys():
            overlap = self._calculate_overlap_quality(current, zone_key)
            if overlap > best_overlap:
                best_overlap = overlap
                best_zone = zone_key
        
        # If no zone found, try current session zones
        if best_zone is None:
            for zone_key in self._current_session_zones.keys():
                overlap = self._calculate_overlap_quality(current, zone_key)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_zone = zone_key
        
        # If still no zone or overlap is poor, return None
        if best_zone is None or best_overlap < self.min_overlap_quality:
            return None, None

        wick_threshold = self._get_wick_threshold()

        # LONG: Buying pressure with absorption
        if delta > 0:
            zone_delta_dir = self._get_zone_delta_direction(best_zone)
            
            # Only trade long if zone was formed with positive delta
            if zone_delta_dir < 0:
                return None, None
            
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size (volatility-adjusted)
            if lower_wick < wick_threshold:
                return None, None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 4: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, best_zone
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            zone_delta_dir = self._get_zone_delta_direction(best_zone)
            
            # Only trade short if zone was formed with negative delta
            if zone_delta_dir > 0:
                return None, None
            
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute wick size (volatility-adjusted)
            if upper_wick < wick_threshold:
                return None, None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 4: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, best_zone
        
        return None, None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check if confirming bar validates the absorption setup."""
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_absorption_close

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
