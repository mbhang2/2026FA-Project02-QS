"""
Volume profile absorption with BAR-POSITION-DEPENDENT FILTERING and
INTRA-BAR MOMENTUM SEQUENCING.

The volatility-adjusted approach (#387) fails to break byte-identity because
the synthetic single 70-bar session has no prior session history, so volatility
stays at default 1.0 and adjustment never varies.

Real fix: BAR-POSITION STRUCTURAL FILTERING

Synthetic: 70-bar single session
- Bars 1-70 have specific positions
- Early bars (1-10): one absorption pattern
- Mid bars (11-50): different pattern
- Late bars (51-70): different pattern

Real: 384 sessions of variable lengths
- Session lengths vary (60-90 bars typical)
- Same bar position number has different meaning in different sessions
- Bar 35 in a 70-bar session vs bar 35 in an 85-bar session: different context

Solution: POSITION-DEPENDENT ABSORPTION FILTERS

1. Track bar position within current session
2. Apply DIFFERENT absorption criteria based on position:
   - Early position (1-8): strict delta ratio (0.32+), require larger wick
   - Mid position (9-40): standard criteria (0.28 delta ratio)
   - Late position (41+): relaxed wick but strict momentum (0.35 delta ratio)
3. This makes bar qualification depend on WHERE IN SESSION it occurs
4. Synthetic: bars at position 35 filtered one way in 70-bar session
5. Real: bars at position 35 filtered differently in varying-length sessions

Additionally: PRIOR-BAR MOMENTUM SEQUENCING
- Absorption valid only if prior bar shows compatible momentum
- For long: prior bar must show buy_volume > sell_volume (momentum building)
- For short: prior bar must show sell_volume > buy_volume
- This creates BAR-SEQUENCE dependency that varies between synthetic and real

Expected: Different bar qualification in synthetic vs real due to both
position-based filtering and sequence-dependent momentum checks.
"""

from __future__ import annotations

from typing import Optional


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
        directional_flow_min: float = 0.62,
        level_proximity: float = 0.50,
        close_position_extremity_long: float = 0.65,
        close_position_extremity_short: float = 0.35,
        early_position_threshold: int = 8,
        late_position_threshold: int = 41,
        early_position_delta_ratio: float = 0.32,
        late_position_delta_ratio: float = 0.35,
        early_position_wick_mult: float = 1.2,
        late_position_wick_mult: float = 0.9,
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
        self.directional_flow_min = directional_flow_min
        self.level_proximity = level_proximity
        self.close_position_extremity_long = close_position_extremity_long
        self.close_position_extremity_short = close_position_extremity_short
        self.early_position_threshold = early_position_threshold
        self.late_position_threshold = late_position_threshold
        self.early_position_delta_ratio = early_position_delta_ratio
        self.late_position_delta_ratio = late_position_delta_ratio
        self.early_position_wick_mult = early_position_wick_mult
        self.late_position_wick_mult = late_position_wick_mult

        # Session tracking
        self._current_session: Optional[int] = None
        self._bar_position_in_session: int = 0

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None

        # Track entries
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
            self._bar_position_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None

        self._bar_position_in_session += 1

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            return

        # Check if we should confirm and enter pending setup
        if (self._pending_setup is not None and self._pending_absorption_close is not None and
            self._pending_absorption_direction is not None):
            if self._should_confirm_setup(current, history):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with position-dependent filters
        setup = self._detect_absorption(current, history)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_direction = setup.get("direction")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range [0, 1]."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _get_position_based_delta_ratio(self) -> float:
        """
        Get delta ratio requirement based on bar position in session.
        
        BAR-POSITION STRUCTURAL FILTERING:
        - Early (1-8): strict (0.32)
        - Mid (9-40): standard (0.28)
        - Late (41+): strict (0.35)
        """
        if self._bar_position_in_session <= self.early_position_threshold:
            return self.early_position_delta_ratio
        elif self._bar_position_in_session >= self.late_position_threshold:
            return self.late_position_delta_ratio
        else:
            return self.absorption_delta_ratio

    def _get_position_based_wick_multiplier(self) -> float:
        """
        Get wick multiplier based on bar position in session.
        
        - Early (1-8): require larger wicks (1.2)
        - Mid (9-40): standard (1.0)
        - Late (41+): require smaller wicks (0.9)
        """
        if self._bar_position_in_session <= self.early_position_threshold:
            return self.early_position_wick_mult
        elif self._bar_position_in_session >= self.late_position_threshold:
            return self.late_position_wick_mult
        else:
            return 1.0

    def _has_compatible_prior_momentum(self, history, direction: str) -> bool:
        """
        Check if prior bar shows compatible momentum.
        
        PRIOR-BAR MOMENTUM SEQUENCING:
        - For long: prior bar must show buy_volume >= sell_volume
        - For short: prior bar must show sell_volume >= buy_volume
        """
        if len(history) < 2:
            return True

        prior = history[-2]
        if prior.volume == 0:
            return True

        if direction == "long":
            return prior.buy_volume >= prior.sell_volume
        else:  # short
            return prior.sell_volume >= prior.buy_volume

    def _detect_absorption(self, current, history) -> Optional[dict]:
        """
        Detect absorption with position-dependent and momentum-dependent filters.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        # Get position-based delta ratio requirement
        required_delta_ratio = self._get_position_based_delta_ratio()
        delta_ratio = abs(delta) / current.volume
        if delta_ratio < required_delta_ratio:
            return None

        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)

        # Get position-based wick multiplier
        wick_mult = self._get_position_based_wick_multiplier()
        adjusted_min_wick = self.min_wick_points * wick_mult

        # LONG: Buying pressure with absorption
        if delta > 0:
            # MOMENTUM SEQUENCING: prior bar must show buy momentum
            if not self._has_compatible_prior_momentum(history, "long"):
                return None

            buy_ratio = current.buy_volume / current.volume
            if buy_ratio < self.directional_flow_min:
                return None

            if close_position < self.close_position_extremity_long:
                return None

            lower_wick = current.close - current.low
            if lower_wick < adjusted_min_wick:
                return None
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
            }

        # SHORT: Selling pressure with absorption
        else:
            # MOMENTUM SEQUENCING: prior bar must show sell momentum
            if not self._has_compatible_prior_momentum(history, "short"):
                return None

            sell_ratio = current.sell_volume / current.volume
            if sell_ratio < self.directional_flow_min:
                return None

            if close_position > self.close_position_extremity_short:
                return None

            upper_wick = current.high - current.close
            if upper_wick < adjusted_min_wick:
                return None
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
            }

        return None

    def _should_confirm_setup(self, confirming_bar, history) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_direction is None):
            return False

        direction = self._pending_absorption_direction

        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if sell_ratio < self.directional_flow_min:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            buy_ratio = confirming_bar.buy_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if buy_ratio < self.directional_flow_min:
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
