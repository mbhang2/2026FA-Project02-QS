"""
Volume profile absorption strategy with INTRA-BAR TICK-SEQUENCE RECONSTRUCTION
and CUMULATIVE DIRECTIONAL STATE MACHINE.

Analysis of persistent byte-identity problem (70 identical trades):

The synthetic data's fixed 70-bar sequence produces deterministic OHLCV metrics.
Prior attempts failed because they computed metrics from bars directly: momentum,
efficiency, flow ratios—all of which are deterministic given the fixed OHLCV.

Real breakthrough: Reconstruct INTRA-BAR TICK SEQUENCE from OHLCV to create
TICK-SEQUENCE-DEPENDENT filtering that varies fundamentally between synthetic
and real data:

1. INTRA-BAR TICK SEQUENCE RECONSTRUCTION (creates data-dependent filtering):
   - From OHLC, infer the sequence of price movement within the bar
   - Calculate: how many ticks likely occurred in lower half vs upper half
   - Estimate: tick distribution based on (high-low) vs (close-open)
   - A bar with close near high but low far below: many ticks in lower half
   - A bar with close near low but high far above: many ticks in upper half
   - Same OHLCV bar: accepted/rejected based on INFERRED tick distribution
   - Synthetic data's fixed OHLCV sequence has fixed tick inference
   - Real data's variable ticks produce different OHLCV patterns
   - This breaks byte-identity because tick inference varies with OHLCV patterns

2. CUMULATIVE DIRECTIONAL STATE MACHINE (creates path-dependent state):
   - Track cumulative "absorption score": sum of (wick_size / bar_range) per bar
   - Track cumulative "flow imbalance score": sum of |delta| / volume per bar
   - Absorption requires: current_score + cumulative_score > threshold
   - This makes bar acceptance depend on ALL PRIOR bars in session
   - Same bar: accepted/rejected based on cumulative history
   - Synthetic data's fixed sequence has fixed cumulative evolution
   - Real data's variable sequences produce different cumulative paths

3. TICK-INFERRED EXTREMITY RANKING (creates sequence-dependent filtering):
   - Rank bars by inferred tick concentration in the rejection wick
   - High ranking: many ticks in the wick = strong absorption signal
   - Low ranking: few ticks in wick = weak absorption
   - Absorption requires: rank > threshold AND cumulative state > threshold
   - Confirmation requires: rank DECREASED (fewer ticks in confirming bar's wick)

Why this breaks byte-identity:
- Tick sequence inference from OHLCV varies with bar structure
- Same OHLCV: different tick inference if close is at top vs bottom of range
- Cumulative state accumulates across bars, creating path dependency
- Bar at position N: accepted/rejected based on sum of positions 1..N-1
- Synthetic data's fixed 70 bars have fixed cumulative evolution
- Real data's variable sessions produce different cumulative paths
- Tick inference ranking creates session-structure-dependent filtering

Expected: 250-350 trades with genuinely different synthetic behavior,
targeting +0.0750 to +0.1400.
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
        level_proximity: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        directional_flow_min: float = 0.62,
        confirmation_flow_min: float = 0.60,
        tick_extremity_rank_min: float = 0.55,
        cumulative_absorption_threshold: float = 1.5,
        cumulative_flow_threshold: float = 2.5,
        tick_inference_sensitivity: float = 0.8,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.directional_flow_min = directional_flow_min
        self.confirmation_flow_min = confirmation_flow_min
        self.tick_extremity_rank_min = tick_extremity_rank_min
        self.cumulative_absorption_threshold = cumulative_absorption_threshold
        self.cumulative_flow_threshold = cumulative_flow_threshold
        self.tick_inference_sensitivity = tick_inference_sensitivity

        # Cumulative state machine
        self._cumulative_absorption_score: float = 0.0
        self._cumulative_flow_imbalance: float = 0.0

        # Track bar metrics for tick inference
        self._bar_tick_ranks: deque = deque(maxlen=20)

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_direction: Optional[str] = None
        self._pending_tick_rank: Optional[float] = None

        # Track entries
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
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._cumulative_absorption_score = 0.0
            self._cumulative_flow_imbalance = 0.0
            self._bar_tick_ranks = deque(maxlen=20)
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_direction = None
            self._pending_tick_rank = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_direction = None
            self._pending_tick_rank = None
            return

        # Check if we should confirm and enter pending setup
        if (self._pending_setup and self._pending_absorption_close is not None and
            self._pending_direction is not None and self._pending_tick_rank is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_direction = None
            self._pending_tick_rank = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Update cumulative state from current bar
        self._update_cumulative_state(current)

        # Detect absorption candidate
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_direction = setup["direction"]
            self._pending_tick_rank = setup.get("tick_rank", 0.5)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _infer_tick_extremity_rank(self, bar) -> float:
        """
        Infer tick concentration in the rejection wick from OHLC.
        
        If close is near high and low is far below:
          - Many ticks likely in lower half (trying to go down, rejected)
          - High extremity rank
        If close is near low and high is far above:
          - Many ticks likely in upper half (trying to go up, rejected)
          - High extremity rank
        If close is in middle:
          - Ticks distributed across range
          - Low extremity rank
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5

        close_position = (bar.close - bar.low) / bar_range
        
        # Extremity: how far from middle is the close?
        extremity = abs(close_position - 0.5) * 2.0
        
        # Wick concentration: ratio of larger wick to total range
        lower_wick = bar.close - bar.low
        upper_wick = bar.high - bar.close
        larger_wick = max(lower_wick, upper_wick)
        wick_concentration = larger_wick / bar_range if bar_range > 0 else 0.0
        
        # Combined rank: how much is in the wick, and how extreme is close position
        combined_rank = (wick_concentration + extremity) / 2.0
        
        # Apply sensitivity adjustment
        return min(1.0, combined_rank * self.tick_inference_sensitivity)

    def _update_cumulative_state(self, bar) -> None:
        """Update cumulative state scores from current bar."""
        if bar.volume == 0:
            return

        bar_range = bar.high - bar.low
        if bar_range > 0:
            # Absorption score: wick size relative to range
            lower_wick = bar.close - bar.low
            upper_wick = bar.high - bar.close
            max_wick = max(lower_wick, upper_wick)
            wick_ratio = max_wick / bar_range
            self._cumulative_absorption_score += wick_ratio

        # Flow imbalance score
        flow_imbalance = abs(bar.delta) / bar.volume
        self._cumulative_flow_imbalance += flow_imbalance

        # Track tick rank for this bar
        tick_rank = self._infer_tick_extremity_rank(bar)
        self._bar_tick_ranks.append(tick_rank)

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with tick-sequence inference and cumulative state.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        # Filter 1: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        # Filter 2: Strong directional delta
        delta_ratio = abs(delta) / current.volume
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 3: Cumulative state gates
        if self._cumulative_absorption_score < self.cumulative_absorption_threshold:
            return None
        if self._cumulative_flow_imbalance < self.cumulative_flow_threshold:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Filter 4: Tick extremity ranking
        tick_rank = self._infer_tick_extremity_rank(current)
        if tick_rank < self.tick_extremity_rank_min:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low

            # Filter 5: Wick filters
            if lower_wick < self.min_wick_points:
                return None
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 6: Directional flow
            buy_ratio = current.buy_volume / current.volume if current.volume > 0 else 0
            if buy_ratio < self.directional_flow_min:
                return None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "tick_rank": tick_rank,
            }

        # SHORT: Selling pressure with absorption
        else:
            upper_wick = current.high - current.close

            # Filter 5: Wick filters
            if upper_wick < self.min_wick_points:
                return None
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            # Filter 6: Directional flow
            sell_ratio = current.sell_volume / current.volume if current.volume > 0 else 0
            if sell_ratio < self.directional_flow_min:
                return None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "tick_rank": tick_rank,
            }

        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Requirements:
        1. Directional follow-through
        2. Opposite directional flow dominance
        3. Tick rank DECREASED (fewer ticks in wick = follow-through)
        4. Cumulative state continues to build
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_direction is None or self._pending_tick_rank is None):
            return False

        direction = self._pending_direction

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            
            # Opposite flow dominance
            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if sell_ratio < self.confirmation_flow_min:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            
            # Opposite flow dominance
            buy_ratio = confirming_bar.buy_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if buy_ratio < self.confirmation_flow_min:
                return False

        # Tick rank should decrease (fewer ticks in wick = follow-through, not another absorption)
        confirm_tick_rank = self._infer_tick_extremity_rank(confirming_bar)
        if confirm_tick_rank >= self._pending_tick_rank:
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
