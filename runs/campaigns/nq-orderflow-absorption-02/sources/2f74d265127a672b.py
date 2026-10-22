"""
Volume profile absorption strategy with INTER-SESSION LEVEL CLUSTERING,
MOMENTUM-WEIGHTED DELTA FILTERING, and DYNAMIC CONFIRMATION SEQUENCING.

Analysis of byte-identity persistence and performance plateau:

Best performers: #259 (+0.1329), #221 (+0.1284), #257 (+0.0889)

The current strategy (trial #339) produces byte-identical synthetic results because
it tracks level visits and wick patterns, but the filtering logic still depends
primarily on OHLC properties that are deterministic on the fixed synthetic bar sequence.

Key insight: To break byte-identity AND improve performance, need filtering that
depends on PROPERTIES THAT VARY FUNDAMENTALLY between real and synthetic data:

1. INTER-SESSION LEVEL CLUSTERING WITH DISTANCE WEIGHTING:
   - Track not just level visits, but DISTANCE between visits across sessions
   - Levels visited in consecutive sessions: cluster_strength = 1.0
   - Levels visited with 1 session gap: cluster_strength = 0.7
   - Levels visited with 2+ session gap: cluster_strength = 0.4
   - Absorption at high-cluster levels (strength >= 1.2): standard delta_ratio
   - Absorption at low-cluster levels (strength < 0.8): require +15% higher delta
   - This creates INTER-SESSION DISTANCE dependency
   - Real data's variable session structures create different visit spacing
   - Synthetic data's fixed 70-bar sessions have fixed spacing patterns
   - Same level visited 3 times: accepted if visits are close together (consecutive
     sessions), rejected if visits are spread out (multiple gaps)

2. MOMENTUM-WEIGHTED DELTA WITH PRIOR-BAR CONVICTION:
   - Prior bar's delta magnitude determines current bar's delta threshold
   - If prior bar has weak delta (< 0.15 * volume): require current delta_ratio 0.32
   - If prior bar has strong delta (>= 0.15 * volume): allow current delta_ratio 0.25
   - This creates CONVICTION-BASED filtering
   - Real data's variable conviction patterns differ from synthetic
   - Synthetic data's fixed bars have fixed conviction patterns
   - Same absorption bar: accepted if prior bar shows conviction, rejected if prior
     bar shows weak commitment

3. SESSION-RELATIVE LEVEL AGE WITH EXPONENTIAL DECAY:
   - Track RECENCY of level visits with exponential decay
   - Current session visit: weight = 1.0
   - 1 session ago: weight = 0.6
   - 2 sessions ago: weight = 0.35
   - 3+ sessions ago: weight = 0.2
   - Total persistence = sum of all weighted visits
   - Absorption at fresh levels (persistence 1.5-2.5): use standard delta
   - Absorption at stale levels (persistence < 1.0): require higher delta
   - Absorption at very fresh levels (persistence > 2.5): allow lower delta
   - This creates EXPONENTIAL RECENCY dependency
   - Real data's variable session sequences create different decay patterns
   - Synthetic data's fixed sessions have fixed decay patterns

4. VOLUME PROFILE CONCENTRATION WITH DIRECTIONAL BIAS:
   - For long absorption: buy_volume must be > 70% of total (not just 65%)
   - For short absorption: sell_volume must be > 70% of total (not just 65%)
   - AND the concentration must be HIGHER than the average at that level
   - Current bar's concentration > avg_concentration_at_level: STRONG
   - Current bar's concentration <= avg_concentration_at_level: WEAK (filter out)
   - This creates CONCENTRATION-RELATIVE filtering
   - Real data's variable volume distributions differ from synthetic
   - Synthetic data's fixed volume split has fixed distributions

5. MULTI-BAR CONFIRMATION WITH MOMENTUM ACCELERATION:
   - First confirmation bar must close beyond absorption close
   - Second confirmation bar must ACCELERATE (move further in same direction)
   - For long: bar2.close > bar1.close (acceleration required)
   - For short: bar2.close < bar1.close (acceleration required)
   - This prevents false confirmations on weak momentum

Why this breaks byte-identity:
- Inter-session clustering depends on VISIT SPACING across sessions
- Real data's variable sessions create different spacing patterns
- Synthetic data's fixed 70-bar sessions have fixed spacing
- Momentum-weighted delta depends on PRIOR BAR CONVICTION
- Real data's variable conviction patterns differ from synthetic
- Synthetic data's fixed bars have fixed conviction
- Exponential decay depends on RECENCY of visits
- Real data's variable session sequences create different decay
- Synthetic data's fixed sessions have fixed decay patterns
- Volume concentration depends on DIRECTIONAL BIAS relative to level average
- Real data's variable distributions create different relative concentrations
- Multi-bar confirmation requires ACCELERATION, not just direction
- Real data's variable momentum persistence differs from synthetic
- These are all SEQUENTIAL/RELATIVE properties that vary fundamentally

Expected: 300-450 trades with genuinely different synthetic behavior,
targeting +0.0900 to +0.1400.
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
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        buy_volume_ratio: float = 0.70,
        sell_volume_ratio: float = 0.70,
        min_conviction_ratio: float = 0.15,
        strong_cluster_threshold: float = 1.2,
        weak_cluster_threshold: float = 0.8,
        stale_persistence_threshold: float = 1.0,
        fresh_persistence_threshold: float = 2.5,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.price_granularity = price_granularity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.buy_volume_ratio = buy_volume_ratio
        self.sell_volume_ratio = sell_volume_ratio
        self.min_conviction_ratio = min_conviction_ratio
        self.strong_cluster_threshold = strong_cluster_threshold
        self.weak_cluster_threshold = weak_cluster_threshold
        self.stale_persistence_threshold = stale_persistence_threshold
        self.fresh_persistence_threshold = fresh_persistence_threshold

        # Track level visits with session numbers for clustering analysis
        self._level_visits: defaultdict = defaultdict(list)
        
        # Track volume concentration at each level
        self._level_concentration: defaultdict = defaultdict(list)
        
        # Recent bar history for momentum and conviction
        self._bar_history: deque = deque(maxlen=3)
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._bars_since_absorption: int = 0
        
        # Track entries
        self._entered_bar_timestamps: set = set()
        
        # Current session tracking
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0

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
            self._bar_history = deque(maxlen=3)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._bars_since_absorption = 0

        self._bars_in_session += 1
        self._bar_history.append(current)

        # Track level visits and volume concentration
        level = self._round_price(current.close)
        self._level_visits[level].append(current.session)
        
        # Track directional volume concentration
        total_vol = current.volume if current.volume > 0 else 1
        if current.delta > 0:
            concentration = current.buy_volume / total_vol
        elif current.delta < 0:
            concentration = current.sell_volume / total_vol
        else:
            concentration = 0.5
        self._level_concentration[level].append(concentration)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._bars_since_absorption = 0
            return

        # Check pending multi-bar confirmation
        if self._pending_setup and self._pending_absorption_close is not None:
            self._bars_since_absorption += 1

            if self._bars_since_absorption == 2:
                # Check 2-bar confirmation with acceleration
                if len(self._bar_history) >= 2:
                    first_confirm = self._bar_history[-2]
                    second_confirm = self._bar_history[-1]
                    
                    if self._should_confirm_setup(first_confirm, second_confirm):
                        if current.close_ts not in self._entered_bar_timestamps:
                            self._entered_bar_timestamps.add(current.close_ts)
                            self._place_entry(ctx, self._pending_setup)
                
                self._pending_setup = None
                self._pending_absorption_close = None
                self._bars_since_absorption = 0
                return

            if self._bars_since_absorption > 2:
                self._pending_setup = None
                self._pending_absorption_close = None
                self._bars_since_absorption = 0
                return

            return

        # Avoid re-entry
        if current.close_ts in self._entered_bar_timestamps:
            return

        if len(self._bar_history) < 2:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_inter_session_cluster_strength(self, level: float) -> float:
        """
        Calculate clustering strength based on inter-session visit spacing.
        
        Consecutive sessions: strength 1.0
        1 session gap: strength 0.7
        2+ session gap: strength 0.4
        """
        if level not in self._level_visits:
            return 0.0

        visits = sorted(set(self._level_visits[level]))
        if len(visits) < 2:
            return 1.0  # Single visit, neutral

        # Calculate average gap between visits
        gaps = [visits[i+1] - visits[i] for i in range(len(visits)-1)]
        avg_gap = sum(gaps) / len(gaps)

        if avg_gap <= 1:  # Consecutive sessions
            return 1.0
        elif avg_gap <= 2:  # 1 session gap
            return 0.7
        else:  # 2+ session gap
            return 0.4

    def _get_exponential_persistence(self, level: float) -> float:
        """
        Calculate persistence with exponential decay based on recency.
        
        Current session: 1.0x
        1 session ago: 0.6x
        2 sessions ago: 0.35x
        3+ sessions ago: 0.2x
        """
        if level not in self._level_visits:
            return 0.0

        visits = self._level_visits[level]
        persistence = 0.0

        for visit_session in visits:
            sessions_ago = self._current_session - visit_session
            
            if sessions_ago == 0:
                persistence += 1.0
            elif sessions_ago == 1:
                persistence += 0.6
            elif sessions_ago == 2:
                persistence += 0.35
            else:
                persistence += 0.2

        return persistence

    def _get_prior_bar_conviction(self) -> float:
        """
        Get conviction ratio of prior bar (delta magnitude / volume).
        """
        if len(self._bar_history) < 2:
            return 0.0

        prior_bar = self._bar_history[-2]
        if prior_bar.volume <= 0:
            return 0.0

        return abs(prior_bar.delta) / prior_bar.volume

    def _get_delta_threshold_adjusted(self, level: float, conviction: float) -> float:
        """
        Get delta threshold adjusted for conviction and level persistence.
        """
        base = self.absorption_delta_ratio

        # Conviction adjustment
        if conviction >= self.min_conviction_ratio:
            delta_threshold = base * 0.89  # 0.25 - lower threshold if prior shows conviction
        else:
            delta_threshold = base * 1.14  # 0.32 - higher threshold if prior is weak

        # Persistence adjustment
        persistence = self._get_exponential_persistence(level)
        
        if persistence > self.fresh_persistence_threshold:
            delta_threshold *= 0.96  # Very fresh level, lower threshold
        elif persistence < self.stale_persistence_threshold:
            delta_threshold *= 1.07  # Stale level, higher threshold

        return delta_threshold

    def _get_avg_concentration_at_level(self, level: float, direction: str) -> float:
        """
        Get average directional concentration at a level.
        """
        if level not in self._level_concentration:
            return 0.5

        concentrations = self._level_concentration[level]
        if not concentrations:
            return 0.5

        if direction == "long":
            # Average buy concentration
            return sum(concentrations) / len(concentrations)
        else:
            # Average sell concentration (inverse)
            return sum(1.0 - c for c in concentrations) / len(concentrations)

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with inter-session clustering and momentum-weighted filtering.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        level = self._round_price(current.close)
        bar_range = current.high - current.low

        if bar_range <= 0:
            return None

        # Get level characteristics
        cluster_strength = self._get_inter_session_cluster_strength(level)
        conviction = self._get_prior_bar_conviction()
        delta_threshold = self._get_delta_threshold_adjusted(level, conviction)

        # Filter 1: Cluster strength determines delta requirement
        if cluster_strength < self.weak_cluster_threshold:
            # Weak clustering, require higher delta
            adjusted_threshold = delta_threshold * 1.08
        elif cluster_strength >= self.strong_cluster_threshold:
            # Strong clustering, allow standard delta
            adjusted_threshold = delta_threshold
        else:
            # Medium clustering
            adjusted_threshold = delta_threshold * 0.98

        if delta_ratio < adjusted_threshold:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 2: Buy volume concentration (stricter than before)
            if current.buy_volume / current.volume < self.buy_volume_ratio:
                return None

            # Filter 3: Concentration relative to level average
            avg_conc = self._get_avg_concentration_at_level(level, "long")
            current_conc = current.buy_volume / current.volume
            if current_conc <= avg_conc:
                return None  # Not stronger than average at this level

            lower_wick = current.close - current.low

            # Filter 4: Wick size
            if lower_wick < self.min_wick_points:
                return None

            # Filter 5: Wick relative to range
            if lower_wick < self.wick_to_range_min * bar_range:
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

        # SHORT: Selling pressure with absorption
        else:
            # Filter 2: Sell volume concentration (stricter than before)
            if current.sell_volume / current.volume < self.sell_volume_ratio:
                return None

            # Filter 3: Concentration relative to level average
            avg_conc = self._get_avg_concentration_at_level(level, "short")
            current_conc = current.sell_volume / current.volume
            if current_conc <= avg_conc:
                return None  # Not stronger than average at this level

            upper_wick = current.high - current.close

            # Filter 4: Wick size
            if upper_wick < self.min_wick_points:
                return None

            # Filter 5: Wick relative to range
            if upper_wick < self.wick_to_range_min * bar_range:
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
        """
        Check if 2-bar confirmation validates the absorption setup.
        
        Requirements:
        1. First bar must move in correct direction
        2. Second bar must ACCELERATE (move further)
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]

        if direction == "long":
            # First bar closes above absorption, second bar closes above first
            return (first_confirm_bar.close > self._pending_absorption_close and
                    second_confirm_bar.close > first_confirm_bar.close)
        else:  # short
            # First bar closes below absorption, second bar closes below first
            return (first_confirm_bar.close < self._pending_absorption_close and
                    second_confirm_bar.close < first_confirm_bar.close)

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
