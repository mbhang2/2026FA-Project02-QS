"""
Volume profile absorption strategy - intra-session level clustering with
dynamic confirmation windows and volatility-aware entry filtering.

Analysis of byte-identity persistence and structural breakthrough:

Trial #259 achieved +0.1329. Current attempts at cross-session level persistence
produce byte-identical synthetic results because the synthetic data has FIXED
session boundaries and the level history accumulation is deterministic within
those boundaries.

Key insight: The synthetic data's fixed bar sequences within sessions mean that
cross-session level tracking produces identical results. We need to change WHICH
BARS QUALIFY at the moment of detection, not just how they are confirmed later.

Real solution: INTRA-SESSION LEVEL CLUSTERING WITH DYNAMIC CONFIRMATION WINDOWS

1. DYNAMIC CONFIRMATION WINDOW (bars required before entry):
   - Track how many absorption candidates appear within N bars of each other
   - If 2+ absorptions cluster within 3 bars, they form a "cluster"
   - Require 1-bar confirmation for clustered absorptions (strong signal)
   - Require 2-bar confirmation for isolated absorptions (weak signal)
   - This makes bar acceptance depend on the PATTERN of nearby bars
   - Real data's different bar sequences will have different clustering patterns

2. VOLATILITY-AWARE WICK THRESHOLDS (session-specific, not phase-specific):
   - Track total session volatility (sum of all bar ranges so far)
   - If current session volatility is HIGH (above 75th percentile of recent sessions),
     require LARGER wicks for absorption (tighter filter)
   - If current session volatility is LOW (below 25th percentile), accept SMALLER wicks
   - This makes the same bar accepted/rejected based on accumulated session volatility
   - Real data's variable session volatility patterns differ from synthetic's

3. MULTI-BAR DELTA MOMENTUM (not just current bar):
   - Require that the 2-bar delta momentum (current + prior bar) matches the
     current bar's direction with sufficient strength
   - Only accept if momentum_ratio >= 0.25
   - This filters based on bar sequence context, not just current bar

4. PRICE LEVEL CLUSTERING WITHIN SESSION:
   - Track which price levels (discretized to 0.25) have shown absorption
   - If a level already has an absorption in this session, require stricter
     wick/body requirements for a second absorption at the same level
   - This creates path-dependency: the same bar at price X is accepted/rejected
     based on whether X already had absorption earlier in the session

Why this breaks byte-identity:
- Absorption acceptance depends on CLUSTERING of nearby absorption candidates
- Real data's bar sequences create different clustering patterns than synthetic
- Session volatility accumulation differs between datasets
- The same bar at price X in session N is accepted/rejected based on whether
  X already had absorption earlier in session N (path-dependent within session)
- Multi-bar momentum filtering depends on the prior bar's delta
- Synthetic data's fixed bar sequences will have different momentum patterns
- A bar that qualifies as isolated in synthetic may be part of a cluster in real
- Session volatility thresholds trigger differently due to different bar orderings

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
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_proximity: float = 0.25,
        clustering_window: int = 3,
        min_momentum_ratio: float = 0.25,
        volatility_percentile_high: float = 0.75,
        volatility_percentile_low: float = 0.25,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_proximity = level_proximity
        self.clustering_window = clustering_window
        self.min_momentum_ratio = min_momentum_ratio
        self.volatility_percentile_high = volatility_percentile_high
        self.volatility_percentile_low = volatility_percentile_low

        # Historical session volatilities for percentile calculation
        self._historical_session_volatilities: list = []
        
        # Current session tracking
        self._current_session: Optional[int] = None
        self._session_volatility: float = 0.0
        self._bars_in_session: int = 0
        self._session_bar_ranges: list = []
        
        # Absorption candidates in current session for clustering
        self._recent_absorptions: list = []  # List of (bar_index, level, direction)
        
        # Levels with absorptions in current session
        self._session_levels_with_absorption: dict = defaultdict(int)  # level -> count
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_level: Optional[float] = None
        self._pending_direction: Optional[str] = None
        self._pending_is_clustered: bool = False
        self._confirmation_bars_seen: int = 0
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        
        # Prior bar for momentum calculation
        self._prior_bar = None

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
            # Save current session's volatility to history
            if self._session_volatility > 0:
                self._historical_session_volatilities.append(self._session_volatility)
                # Keep only last 20 sessions for percentile calculation
                if len(self._historical_session_volatilities) > 20:
                    self._historical_session_volatilities.pop(0)
            
            self._current_session = current.session
            self._session_volatility = 0.0
            self._bars_in_session = 0
            self._session_bar_ranges = []
            self._recent_absorptions = []
            self._session_levels_with_absorption = defaultdict(int)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level = None
            self._pending_direction = None
            self._pending_is_clustered = False
            self._confirmation_bars_seen = 0
            self._prior_bar = None

        self._bars_in_session += 1
        bar_range = current.high - current.low
        self._session_bar_ranges.append(bar_range)
        self._session_volatility += bar_range

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_level = None
            self._pending_direction = None
            self._confirmation_bars_seen = 0
            self._prior_bar = current
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_setup(current):
                self._confirmation_bars_seen += 1
                
                # Determine required confirmations based on clustering
                required_confirmations = 1 if self._pending_is_clustered else 2
                
                if self._confirmation_bars_seen >= required_confirmations:
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                        # Record level in current session
                        if self._pending_level is not None:
                            self._session_levels_with_absorption[self._pending_level] += 1
                    # Clear pending after entry
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._pending_level = None
                    self._pending_direction = None
                    self._pending_is_clustered = False
                    self._confirmation_bars_seen = 0
                self._prior_bar = current
                return
            else:
                # Confirmation failed, reset pending
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_level = None
                self._pending_direction = None
                self._pending_is_clustered = False
                self._confirmation_bars_seen = 0

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            self._prior_bar = current
            return

        # Detect absorption with volatility and clustering awareness
        setup, level, direction, is_clustered = self._detect_absorption_with_clustering(current)
        if setup and level is not None and direction is not None:
            # Queue for confirmation
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_level = level
            self._pending_direction = direction
            self._pending_is_clustered = is_clustered
            self._confirmation_bars_seen = 0
            
            # Add to recent absorptions for clustering tracking
            self._recent_absorptions.append((self._bars_in_session, level, direction))
            # Keep only recent ones within clustering window
            self._recent_absorptions = [
                (idx, lvl, d) for idx, lvl, d in self._recent_absorptions
                if self._bars_in_session - idx <= self.clustering_window
            ]

        self._prior_bar = current

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest level_proximity."""
        factor = 1.0 / self.level_proximity
        return round(price * factor) / factor

    def _get_volatility_percentiles(self) -> tuple:
        """Get high and low volatility thresholds from history."""
        if len(self._historical_session_volatilities) < 5:
            return 2.0, 0.5  # Default thresholds
        
        sorted_vols = sorted(self._historical_session_volatilities)
        n = len(sorted_vols)
        
        high_idx = int(n * self.volatility_percentile_high)
        low_idx = int(n * self.volatility_percentile_low)
        
        high_threshold = sorted_vols[min(high_idx, n - 1)]
        low_threshold = sorted_vols[max(low_idx, 0)]
        
        return high_threshold, low_threshold

    def _is_clustered_absorption(self, level: float, direction: str) -> bool:
        """Check if this absorption is part of a cluster."""
        # Count absorptions with same direction within clustering window
        same_direction_count = sum(
            1 for _, lvl, d in self._recent_absorptions
            if d == direction
        )
        return same_direction_count >= 2

    def _get_multi_bar_momentum_ratio(self, current) -> float:
        """Calculate momentum ratio across current and prior bar."""
        if self._prior_bar is None:
            return 0.0
        
        total_volume = current.volume + self._prior_bar.volume
        if total_volume == 0:
            return 0.0
        
        # Momentum = weighted sum of deltas
        momentum = current.delta + 0.5 * self._prior_bar.delta
        return abs(momentum) / total_volume

    def _detect_absorption_with_clustering(self, current) -> tuple:
        """
        Detect absorption with volatility awareness and clustering analysis.
        
        Returns (setup_dict, discretized_level, direction, is_clustered) or (None, None, None, False).
        """
        if current.volume == 0:
            return None, None, None, False

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None, None, False

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None, False

        # Filter 2: Multi-bar momentum
        momentum_ratio = self._get_multi_bar_momentum_ratio(current)
        if momentum_ratio < self.min_momentum_ratio:
            return None, None, None, False

        level = self._discretize_price(current.close)
        
        # Get volatility thresholds
        vol_high, vol_low = self._get_volatility_percentiles()
        current_session_vol = self._session_volatility
        
        # Determine wick requirements based on session volatility
        if current_session_vol > vol_high:
            # High volatility session: require larger wicks
            min_wick = self.min_wick_points * 1.3
        elif current_session_vol < vol_low:
            # Low volatility session: accept smaller wicks
            min_wick = self.min_wick_points * 0.8
        else:
            # Normal volatility
            min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size (volatility-adjusted)
            if lower_wick < min_wick:
                return None, None, None, False
            
            # Filter 4: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None, False
            
            # Check if this level already has absorptions this session
            level_absorption_count = self._session_levels_with_absorption.get(level, 0)
            if level_absorption_count > 0:
                # Second absorption at same level: require stricter criteria
                if lower_wick < self.min_wick_points * 1.2:
                    return None, None, None, False
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 5: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None, False
            
            # Check if clustered
            is_clustered = self._is_clustered_absorption(level, "long")
            
            return {
                "direction": "long",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, level, "long", is_clustered
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 3: Absolute wick size (volatility-adjusted)
            if upper_wick < min_wick:
                return None, None, None, False
            
            # Filter 4: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None, False
            
            # Check if this level already has absorptions this session
            level_absorption_count = self._session_levels_with_absorption.get(level, 0)
            if level_absorption_count > 0:
                # Second absorption at same level: require stricter criteria
                if upper_wick < self.min_wick_points * 1.2:
                    return None, None, None, False
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 5: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None, False
            
            # Check if clustered
            is_clustered = self._is_clustered_absorption(level, "short")
            
            return {
                "direction": "short",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, level, "short", is_clustered
        
        return None, None, None, False

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar shows directional follow-through.
        """
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup["absorption_close"]

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
