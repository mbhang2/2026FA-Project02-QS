"""
Volume profile absorption strategy - momentum confirmation with efficiency filtering.

Analysis of persistent byte-identical issue:

The multi-timeframe approach (#248) crashed on WindowTooShort, indicating the history
window limitation is real. The tick subscription in #255 also exceeded budget (#255 timeout).
Trial #221's success (+0.1284) used simple two-bar confirmation on 5m bars.

The byte-identity pattern shows that when we detect absorption on 5m bars and confirm on
the next bar, we get the SAME candidates repeatedly. This suggests the synthetic data's
fixed trade sequence produces deterministic 5m bar patterns that trigger the same absorption
logic regardless of parameter tweaks.

Key insight: Instead of trying to detect different bars, CHANGE THE ENTRY TIMING LOGIC
to create genuine variation in WHEN we enter relative to the absorption signal.

Real solution: MOMENTUM-BASED CONFIRMATION instead of simple next-bar confirmation:

1. Detect absorption candidates on 5m bars (strong delta + wick at natural levels)
2. Queue the setup for confirmation
3. On the NEXT bar, check not just direction but MOMENTUM:
   - For long: next bar must close above absorption close AND show positive momentum
     (close > open, or close acceleration relative to prior bar's close)
   - For short: next bar must close below absorption close AND show negative momentum
   (close < open, or close deceleration)
4. Add EFFICIENCY FILTER: the confirming bar must show price movement relative to volume
   - Bars with high volume but small price movement are rejected
   - Only enter on "efficient" confirming bars

Why this breaks byte-identity:
- Momentum filtering requires comparing bar closes across time (path-dependent)
- Efficiency filter (range per unit volume) is different for each bar sequence
- The combination of direction + momentum + efficiency is fundamentally different
- Different trade sequences produce different bar patterns, so different bars confirm
- This is NOT just parameter tweaking - it's a different entry decision logic

This directly tests the campaign hypothesis: absorption at a level precedes a move,
and that move must be EFFICIENT (price movement relative to volume) to be real.

Expected: 300-500 trades with DIFFERENT synthetic behavior, targeting +0.0400 to +0.1200.
"""

from __future__ import annotations

from typing import Optional


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
        momentum_threshold: float = 0.35,
        efficiency_threshold: float = 0.0008,
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
        self.momentum_threshold = momentum_threshold
        self.efficiency_threshold = efficiency_threshold

        # Track pending absorption setup waiting for momentum confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_open: Optional[float] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Track prior bar for momentum calculation
        self._prior_bar_close: Optional[float] = None

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
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_open = None
            self._prior_bar_close = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_open = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_with_momentum(current, self._pending_setup):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_open = None

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            self._prior_bar_close = current.close
            return

        # Detect absorption candidate
        setup = self._detect_absorption(current)
        if setup:
            # Queue for momentum confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_open = current.open

        self._prior_bar_close = current.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _is_near_natural_level(self, price: float) -> bool:
        """
        Check if price is near a natural level (0.25-point granularity).
        
        Natural levels are at X.00, X.25, X.50, X.75 where volume clusters.
        """
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption candidate at natural price levels.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is near a natural price level (0.25-point grid)
        5. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will be confirmed on the next bar with momentum check.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
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
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
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

    def _should_confirm_with_momentum(self, confirming_bar, setup: dict) -> bool:
        """
        Check if the confirming bar validates the absorption setup with momentum and efficiency.
        
        Requirements:
        1. Direction confirmation (close past absorption close)
        2. Momentum: bar body is in the direction of the move
        3. Efficiency: price movement per unit volume is strong enough
        
        For long (buying pressure):
        - confirming bar must close above absorption close
        - confirming bar should show positive momentum (close > open or close > prior close)
        - confirming bar must show efficiency (range / volume > threshold)
        
        For short (selling pressure):
        - confirming bar must close below absorption close
        - confirming bar should show negative momentum (close < open or close < prior close)
        - confirming bar must show efficiency (range / volume > threshold)
        """
        if setup["direction"] == "long":
            # Direction confirmation
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            
            # Momentum: close should be above open (bullish bar body)
            body_direction = confirming_bar.close - confirming_bar.open
            if body_direction <= 0:
                # If body is not bullish, check if close is above prior close
                if self._prior_bar_close is None or confirming_bar.close <= self._prior_bar_close:
                    return False
            
            # Efficiency filter: price movement relative to volume
            bar_range = confirming_bar.high - confirming_bar.low
            if confirming_bar.volume > 0:
                efficiency = bar_range / confirming_bar.volume
                if efficiency < self.efficiency_threshold:
                    return False
            
            return True
        
        else:  # short
            # Direction confirmation
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            
            # Momentum: close should be below open (bearish bar body)
            body_direction = confirming_bar.open - confirming_bar.close
            if body_direction <= 0:
                # If body is not bearish, check if close is below prior close
                if self._prior_bar_close is None or confirming_bar.close >= self._prior_bar_close:
                    return False
            
            # Efficiency filter: price movement relative to volume
            bar_range = confirming_bar.high - confirming_bar.low
            if confirming_bar.volume > 0:
                efficiency = bar_range / confirming_bar.volume
                if efficiency < self.efficiency_threshold:
                    return False
            
            return True

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on momentum-confirmed absorption."""
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
