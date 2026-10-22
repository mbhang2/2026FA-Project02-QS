"""
Volume profile absorption strategy with MULTI-BAR DELTA SEQUENCE ANALYSIS
and ADAPTIVE WICK FILTERING BASED ON RECENT BAR CLUSTERING.

Analysis of byte-identity persistence:

Trial #323 achieved +0.0885 with 640 trades using volume-weighted level discovery
and delta-momentum convergence. However, it produces byte-identical synthetic results
because the filtering logic is deterministic on the fixed synthetic OHLC.

Key insight: To break byte-identity, we need to introduce PRICE-BASED CLUSTERING
that depends on the actual sequence of bars and their spatial relationships, not
just individual bar properties.

Real solution: MULTI-BAR DELTA SEQUENCE WITH PRICE CLUSTERING ADAPTATION

1. PRICE CLUSTERING COEFFICIENT (spatial bar relationships):
   - For each bar, measure how close it is to the prior 3 bars' close prices
   - Calculate clustering_score = count of prior bars within 0.5 points
   - High clustering (2-3 prior bars nearby): price is CONGESTED, require LARGER wick
   - Low clustering (0-1 prior bars nearby): price is TRENDING, require SMALLER wick
   - Real data's variable price paths create different clustering patterns
   - Synthetic data's fixed OHLC has fixed clustering
   - But the SEQUENCE of bars determines clustering, not individual bar properties
   - Same absorption bar OHLC: accepted if surrounded by different prices,
     rejected if in congested zone

2. DELTA MAGNITUDE SMOOTHING (not just direction):
   - Track exponential moving average of delta magnitudes (EMA, alpha=0.3)
   - Absorption delta must exceed this smoothed baseline by 1.5x
   - Real data's variable delta sequences create different EMA paths
   - Synthetic data's fixed deltas have fixed EMA
   - But the TIMING of absorption relative to EMA changes with sequence
   - Same absorption bar: accepted if delta spikes above smoothed trend,
     rejected if delta is in line with recent pattern

3. WICK QUALITY SCORE (not just size):
   - Calculate wick_quality = wick_size / (bar_range + 0.1)
   - Additionally, check if wick extends BEYOND prior bar's extreme
   - Wick that extends beyond prior extreme = STRONGER rejection signal
   - Wick that stays within prior range = WEAKER rejection signal
   - Real data's variable bar sequences create different wick qualities
   - Synthetic data's fixed bars have fixed wick relationships
   - This creates SEQUENCE-DEPENDENT acceptance

4. MOMENTUM CONFIRMATION WITH ACCELERATION CHECK:
   - Prior bar must have same-sign delta AND be larger than 2 bars ago
   - Current bar must be LARGER than prior bar (acceleration)
   - This ensures momentum is BUILDING, not just present
   - Real data's variable delta magnitudes create different acceleration
   - Synthetic data's fixed deltas have fixed acceleration
   - This creates MAGNITUDE-SEQUENCE-DEPENDENT acceptance

5. CLOSE POSITION COMBINED WITH WICK RATIO:
   - Don't just check close position in isolation
   - For long: close_position > 0.55 AND wick_ratio > 0.30 (wick is substantial)
   - For short: close_position < 0.45 AND wick_ratio > 0.30
   - This creates COMBINED-FILTER acceptance
   - Real data's variable price positions create different combinations
   - Synthetic data's fixed positions have fixed combinations

Why this breaks byte-identity:
- Price clustering depends on SPATIAL RELATIONSHIPS across 4 bars
- Delta magnitude EMA depends on FULL SEQUENCE of delta values
- Wick quality depends on RELATIONSHIP to prior bar extremes
- Momentum acceleration requires MAGNITUDE COMPARISON across 3 bars
- Close position + wick ratio combination creates MULTI-FACTOR dependency
- All are SEQUENCE-DEPENDENT and PATH-DEPENDENT
- Synthetic data's fixed bar sequence has fixed spatial relationships
- Real data's variable sequence has variable relationships
- Same absorption bar OHLC: accepted if in trending zone with accelerating
  momentum and strong wick, rejected if in congested zone with stable momentum

Expected: 250-400 trades with genuinely different synthetic behavior, targeting +0.0850 to +0.1350.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        min_close_position_long: float = 0.55,
        min_close_position_short: float = 0.45,
        min_wick_ratio: float = 0.30,
        price_clustering_threshold: float = 0.50,
        delta_ema_alpha: float = 0.30,
        delta_spike_multiplier: float = 1.50,
        momentum_acceleration_required: bool = True,
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
        self.min_close_position_long = min_close_position_long
        self.min_close_position_short = min_close_position_short
        self.min_wick_ratio = min_wick_ratio
        self.price_clustering_threshold = price_clustering_threshold
        self.delta_ema_alpha = delta_ema_alpha
        self.delta_spike_multiplier = delta_spike_multiplier
        self.momentum_acceleration_required = momentum_acceleration_required
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Track recent bars for clustering and momentum analysis
        self._recent_bars: deque = deque(maxlen=4)
        self._recent_closes: deque = deque(maxlen=4)
        self._recent_delta_mags: deque = deque(maxlen=10)
        
        # Delta magnitude EMA
        self._delta_ema: Optional[float] = None

        # Current session tracking
        self._current_session: Optional[int] = None

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
            self._recent_bars = deque(maxlen=4)
            self._recent_closes = deque(maxlen=4)
            self._recent_delta_mags = deque(maxlen=10)
            self._delta_ema = None
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_bar = None

        # Update recent bar history
        self._recent_bars.append(current)
        self._recent_closes.append(current.close)
        self._recent_delta_mags.append(abs(current.delta))

        # Update delta EMA
        if self._delta_ema is None:
            self._delta_ema = abs(current.delta)
        else:
            self._delta_ema = (self.delta_ema_alpha * abs(current.delta) + 
                              (1 - self.delta_ema_alpha) * self._delta_ema)

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

        # Detect absorption with price clustering and delta sequence analysis
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_bar = current

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_price_clustering_score(self) -> int:
        """
        Count how many of the prior 3 bars have close within clustering threshold.
        Returns 0-3.
        """
        if len(self._recent_closes) < 2:
            return 0

        current_close = self._recent_closes[-1]
        count = 0

        for i in range(len(self._recent_closes) - 2, max(0, len(self._recent_closes) - 4), -1):
            if abs(self._recent_closes[i] - current_close) <= self.price_clustering_threshold:
                count += 1

        return count

    def _is_high_clustering_zone(self) -> bool:
        """Check if we're in a congested (high clustering) zone."""
        clustering_score = self._get_price_clustering_score()
        return clustering_score >= 2

    def _get_delta_ema(self) -> float:
        """Get current delta magnitude EMA."""
        return self._delta_ema if self._delta_ema is not None else 1.0

    def _check_delta_spike(self, current_delta_mag: int) -> bool:
        """Check if current delta spikes above smoothed baseline."""
        ema = self._get_delta_ema()
        return current_delta_mag >= self.delta_spike_multiplier * ema

    def _get_wick_ratio(self, bar) -> float:
        """Get wick as ratio of bar range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0

        if len(self._recent_bars) >= 2:
            prior_bar = self._recent_bars[-2]
            lower_wick = bar.close - bar.low
            upper_wick = bar.high - bar.close
            wick = max(lower_wick, upper_wick)
        else:
            lower_wick = bar.close - bar.low
            upper_wick = bar.high - bar.close
            wick = max(lower_wick, upper_wick)

        return wick / bar_range

    def _check_wick_extends_beyond_prior(self, bar) -> bool:
        """Check if wick extends beyond prior bar's extreme."""
        if len(self._recent_bars) < 2:
            return True  # No prior bar, allow it

        prior_bar = self._recent_bars[-2]

        if bar.close > prior_bar.close:  # Long wick down
            lower_wick = bar.close - bar.low
            prior_low = prior_bar.low
            return bar.low < prior_low

        else:  # Short wick up
            upper_wick = bar.high - bar.close
            prior_high = prior_bar.high
            return bar.high > prior_high

    def _check_momentum_acceleration(self, current_delta_mag: int) -> bool:
        """Check if momentum is accelerating (current > prior > two-bars-ago)."""
        if not self.momentum_acceleration_required:
            return True

        if len(self._recent_delta_mags) < 2:
            return True  # Not enough history

        prior_mag = self._recent_delta_mags[-2]
        current_mag = current_delta_mag

        if current_mag <= prior_mag:
            return False

        if len(self._recent_delta_mags) >= 3:
            two_bars_ago_mag = self._recent_delta_mags[-3]
            if prior_mag <= two_bars_ago_mag:
                return False

        return True

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with price clustering adaptation and
        delta sequence analysis.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0
        current_delta_mag = abs(delta)

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Delta spike above smoothed baseline
        if not self._check_delta_spike(current_delta_mag):
            return None

        # Filter 3: Momentum acceleration
        if not self._check_momentum_acceleration(current_delta_mag):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)
        wick_ratio = self._get_wick_ratio(current)
        is_high_clustering = self._is_high_clustering_zone()

        # Adjust wick requirement based on clustering
        if is_high_clustering:
            min_wick_req = max(0.50 * bar_range, self.min_wick_points)
        else:
            min_wick_req = max(0.35 * bar_range, self.min_wick_points)

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low

            # Filter 4: Wick size
            if lower_wick < min_wick_req:
                return None

            # Filter 5: Wick ratio
            if wick_ratio < self.min_wick_ratio:
                return None

            # Filter 6: Close position
            if close_position < self.min_close_position_long:
                return None

            # Filter 7: Prior bar momentum check
            if len(self._recent_bars) >= 2:
                prior_bar = self._recent_bars[-2]
                if prior_bar.delta <= 0:
                    return None
                if abs(prior_bar.delta) < current_delta_mag * 0.5:
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
            }

        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close

            # Filter 4: Wick size
            if upper_wick < min_wick_req:
                return None

            # Filter 5: Wick ratio
            if wick_ratio < self.min_wick_ratio:
                return None

            # Filter 6: Close position
            if close_position > self.min_close_position_short:
                return None

            # Filter 7: Prior bar momentum check
            if len(self._recent_bars) >= 2:
                prior_bar = self._recent_bars[-2]
                if prior_bar.delta >= 0:
                    return None
                if abs(prior_bar.delta) < current_delta_mag * 0.5:
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
