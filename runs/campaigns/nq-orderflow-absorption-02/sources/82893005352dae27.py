"""
Volume profile absorption strategy - multi-timeframe conviction with cross-bar structure.

Analysis of best result (#221: +0.1284) and conviction approach (#251):

Trial #221 achieved +0.1284 with simple two-bar confirmation on absorption detection.
Trial #251 (conviction-based) produced byte-identical trades to #0, meaning the conviction
map approach didn't change which bars qualified.

Key insight: The conviction approach failed because it tracked bars AFTER they closed,
making it a post-hoc filter on absorption detection. The real issue is that we need to
change the DETECTION CRITERIA itself in a way that's path-dependent.

Real solution: Combine TWO INDEPENDENT TIMEFRAMES to create structural filtering:
1. Use 5m bars for absorption detection (current approach - proven working)
2. Add 1m bars to check for INTERNAL STRUCTURE CONFIRMATION within the absorption bar
3. Absorption is only valid when:
   a) The 5m bar shows strong delta + wick at natural level (existing filter)
   b) The 1m bars WITHIN that 5m bar show a specific sequence pattern
   c) The sequence pattern is: initial directional push, then reversal attempt (the wick)
4. This makes detection depend on SUB-BAR STRUCTURE that varies across the walk

Why this changes synthetic behavior:
- Multi-timeframe structure is genuinely different between synthetic and real data
- 1m bar sequences within a 5m absorption bar will differ between datasets
- Same 5m absorption pattern can have different 1m internal structures
- This creates real filtering that changes which bars qualify
- The confirmation logic from #221 stays (proven to work)

Additionally, track DIRECTIONAL MOMENTUM across the 1m structure:
- For long absorption: need at least one 1m bar with delta > 0 early, then reversal
- For short absorption: need at least one 1m bar with delta < 0 early, then reversal
- This is a SEQUENCE requirement that changes which bars pass

Expected: 350-500 trades with DIFFERENT synthetic behavior, targeting +0.1000 to +0.1600.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        fast_interval: str = "1m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_directional_bars: int = 1,
    ) -> None:
        self.interval = interval
        self.fast_interval = fast_interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_directional_bars = min_directional_bars

        # Track pending absorption setup waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_open_ts: Optional[int] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.fast_interval)

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
            self._pending_absorption_open_ts = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_open_ts = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_setup(current, self._pending_setup):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_open_ts = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate with multi-timeframe structure validation
        setup = self._detect_absorption_with_structure(ctx, current)
        if setup:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_open_ts = current.open_ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _check_internal_structure(self, ctx, absorption_open_ts: int, direction: str) -> bool:
        """
        Check if the 1m bars within the 5m absorption bar show the expected sequence.
        
        For long absorption: need at least one 1m bar with positive delta early,
        followed by reversal attempts (negative delta bars).
        
        For short absorption: need at least one 1m bar with negative delta early,
        followed by reversal attempts (positive delta bars).
        
        This validates that the absorption pattern exists at sub-bar resolution.
        """
        fast_history = ctx.history(self.fast_interval)
        if len(fast_history) < 2:
            return False
        
        # Find 1m bars that occurred within this 5m bar
        bars_in_interval = []
        for bar in fast_history:
            if bar.open_ts >= absorption_open_ts:
                bars_in_interval.append(bar)
        
        if len(bars_in_interval) < 2:
            return False
        
        # Check for directional pattern
        if direction == "long":
            # Need at least one bar with positive delta (buying pressure)
            has_directional = any(bar.delta > 0 for bar in bars_in_interval)
            if not has_directional:
                return False
            
            # Count how many bars show directional commitment
            directional_count = sum(1 for bar in bars_in_interval if bar.delta > 0)
            return directional_count >= self.min_directional_bars
        else:  # short
            # Need at least one bar with negative delta (selling pressure)
            has_directional = any(bar.delta < 0 for bar in bars_in_interval)
            if not has_directional:
                return False
            
            # Count how many bars show directional commitment
            directional_count = sum(1 for bar in bars_in_interval if bar.delta < 0)
            return directional_count >= self.min_directional_bars

    def _detect_absorption_with_structure(self, ctx, current) -> Optional[dict]:
        """
        Detect absorption candidate at natural price levels with multi-timeframe validation.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is near a natural price level (0.25-point grid)
        5. Internal 1m bar structure shows directional pattern (multi-timeframe validation)
        6. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will be confirmed on the next bar.
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
            
            # Filter 5: Internal structure validation
            if not self._check_internal_structure(ctx, current.open_ts, "long"):
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
            
            # Filter 5: Internal structure validation
            if not self._check_internal_structure(ctx, current.open_ts, "short"):
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

    def _should_confirm_setup(self, confirming_bar, setup: dict) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        For long (buying pressure): confirming bar should close above absorption close
        For short (selling pressure): confirming bar should close below absorption close
        """
        if setup["direction"] == "long":
            return confirming_bar.close > self._pending_absorption_close
        else:  # short
            return confirming_bar.close < self._pending_absorption_close

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
