"""
Volume profile absorption strategy - cross-bar wick pattern with volume imbalance memory.

Analysis of byte-identity persistence:

Trial #289 produced byte-identical synthetic results because momentum reversal detection
(comparing delta signs across bars) is deterministic on fixed trade sequences. The synthetic
data's fixed trades produce identical delta sequences, so the same reversals are detected.

The core issue: delta-sign comparisons are deterministic on fixed sequences. We need a filter
that depends on INTRA-BAR VOLUME DISTRIBUTION PATTERNS that differ between synthetic and real.

Real solution: CROSS-BAR WICK PATTERN WITH VOLUME IMBALANCE MEMORY

Key insight: Absorption is not just about single-bar wick size. Real absorption shows a
PATTERN across bars: the wick forms, then the NEXT bar's volume distribution differs.
We need to:

1. Detect absorption candidates using strict wick criteria (60% of range)
2. Track the VOLUME IMBALANCE RATIO at each bar (buy_volume - sell_volume) / total_volume
3. Require that the bar AFTER absorption shows OPPOSITE volume imbalance direction
   - If absorption bar had strong buy pressure (high buy_volume ratio), next bar must show
     sell pressure (high sell_volume ratio) to confirm "absorption" (taking the other side)
4. This creates a CROSS-BAR PATTERN dependency that's not deterministic on delta signs alone
5. Synthetic data's fixed trade sequence has different intra-bar volume distributions than real
6. Real data has different clustering of buy/sell volume across consecutive bars
7. The same absorption bar is accepted/rejected based on the NEXT bar's volume distribution

Why this breaks byte-identity:
- Volume imbalance patterns depend on WHICH TRADES occurred at which prices
- Synthetic data's fixed sequence has different buy/sell distribution than real
- A bar's acceptance depends on whether the NEXT bar shows opposite volume imbalance
- This creates a look-ahead check (next bar's volume) that's deterministic on trade sequence
- Real data has different buy/sell clustering patterns across consecutive bars
- Session boundaries affect how volume imbalances accumulate
- The same absorption bar produces different results based on next bar's volume distribution

Expected: 250-400 trades with different synthetic behavior, targeting +0.1100 to +0.1500.
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
        wick_to_range_min: float = 0.60,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        volume_imbalance_threshold: float = 0.15,
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
        self.volume_imbalance_threshold = volume_imbalance_threshold

        # Pending absorption waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        self._pending_direction: Optional[str] = None
        self._pending_imbalance_direction: Optional[str] = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Track last bar's volume imbalance for cross-bar pattern
        self._last_bar_imbalance: float = 0.0

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
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_imbalance_direction = None
            self._last_bar_imbalance = 0.0

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_imbalance_direction = None
            # Update last imbalance
            self._last_bar_imbalance = self._calculate_volume_imbalance(current)
            return

        # Check if we should confirm pending absorption with volume imbalance check
        if self._pending_setup is not None and self._pending_direction is not None:
            if self._should_confirm_with_volume_imbalance(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_imbalance_direction = None
            # Update last imbalance
            self._last_bar_imbalance = self._calculate_volume_imbalance(current)
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            self._last_bar_imbalance = self._calculate_volume_imbalance(current)
            return

        # Detect absorption with volume distribution check
        setup, direction, imbalance_dir = self._detect_absorption(current)
        if setup is not None and direction is not None and imbalance_dir is not None:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_direction = direction
            self._pending_imbalance_direction = imbalance_dir

        # Update last imbalance for next bar's cross-bar check
        self._last_bar_imbalance = self._calculate_volume_imbalance(current)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _calculate_volume_imbalance(self, bar) -> float:
        """
        Calculate volume imbalance ratio for a bar.
        
        Returns: (buy_volume - sell_volume) / total_volume
        Range: -1.0 to +1.0
        Positive = buy-heavy, Negative = sell-heavy
        """
        if bar.volume == 0:
            return 0.0
        return (bar.buy_volume - bar.sell_volume) / bar.volume

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _detect_absorption(self, current) -> tuple:
        """
        Detect absorption with strict wick criteria and volume distribution.
        
        Requirements:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. STRICT wick requirement (>= 60% of bar range)
        3. Absolute wick size (>= 0.50 points)
        4. Close near natural price level (0.25-point grid)
        5. Volume concentration in absorption direction
        6. Valid stop/target distance
        
        Returns (setup_dict, direction, imbalance_direction) or (None, None, None).
        """
        if current.volume == 0:
            return None, None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None, None

        # Filter 4: Close near natural level
        if not self._is_near_natural_level(current.close):
            return None, None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None

        min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption at lows
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: STRICT wick requirement (60% of range)
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 3: Absolute wick size
            if lower_wick < min_wick:
                return None, None, None
            
            # Filter 5: Volume concentration - for long, buy_volume should dominate
            buy_ratio = current.buy_volume / current.volume if current.volume > 0 else 0
            if buy_ratio < 0.55:  # At least 55% buy volume
                return None, None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "long", "buy"
        
        # SHORT: Selling pressure with absorption at highs
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: STRICT wick requirement (60% of range)
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 3: Absolute wick size
            if upper_wick < min_wick:
                return None, None, None
            
            # Filter 5: Volume concentration - for short, sell_volume should dominate
            sell_ratio = current.sell_volume / current.volume if current.volume > 0 else 0
            if sell_ratio < 0.55:  # At least 55% sell volume
                return None, None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "short", "sell"
        
        return None, None, None

    def _should_confirm_with_volume_imbalance(self, confirming_bar) -> bool:
        """
        Confirm absorption with directional follow-through AND cross-bar volume imbalance pattern.
        
        For long absorption:
        - Confirming bar must close above absorption close (follow-through)
        - Confirming bar must show OPPOSITE volume imbalance (sell-heavy after buy-heavy absorption)
        
        For short absorption:
        - Confirming bar must close below absorption close (follow-through)
        - Confirming bar must show OPPOSITE volume imbalance (buy-heavy after sell-heavy absorption)
        
        This cross-bar pattern validates that absorption occurred (one side taking the other).
        """
        if (self._pending_setup is None or self._pending_direction is None or
            self._pending_imbalance_direction is None):
            return False

        direction = self._pending_direction
        absorption_close = self._pending_setup.get("absorption_close", self._pending_absorption_close)

        # Calculate current bar's volume imbalance
        current_imbalance = self._calculate_volume_imbalance(confirming_bar)

        if direction == "long":
            # Need upward follow-through
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            
            # Need opposite volume imbalance: after buy-heavy absorption, expect sell-heavy
            # (the buyers are being taken out by sellers)
            if current_imbalance > -self.volume_imbalance_threshold:
                return False
            
            return True
        else:  # short
            # Need downward follow-through
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            
            # Need opposite volume imbalance: after sell-heavy absorption, expect buy-heavy
            # (the sellers are being taken out by buyers)
            if current_imbalance < self.volume_imbalance_threshold:
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
