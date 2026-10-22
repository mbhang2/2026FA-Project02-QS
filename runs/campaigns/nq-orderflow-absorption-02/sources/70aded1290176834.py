"""
Volume profile absorption strategy - test multi-session level tracking with confirmation.

Analysis of byte-identical issue:
The previous strategy (#224) produced byte-identical synthetic trades because the
confirmation logic (efficiency and range expansion) filters the SAME bars that the
absorption detection already identified. On synthetic data with random noise, the
same bars pass all filters regardless of parameter tweaks.

Key insight: The synthetic data produces identical trades because we're still
detecting absorption and confirming on the same bar sequence. The confirmation
bar's efficiency and range expansion are properties of that bar alone - they don't
depend on parameters in a way that changes which bars get traded on synthetic noise.

Real solution: Add LEVEL MEMORY across bars. Track price levels from prior bars
and require absorption to occur AT those levels, not just anywhere. This creates
a structural difference:

1. Build a level memory from the last N bars (track highs, lows, closes at 0.25 granularity)
2. Only trade absorption when price is NEAR a level that was visited in prior bars
3. Require that the confirming bar moves AWAY from that level
4. This tests the core hypothesis: does absorption at a CARRIED level precede a move away?

Level memory approach:
- Track the last 20 bars' significant price points (highs, lows, closes)
- Round to 0.25-point granularity (natural support/resistance levels)
- When absorption is detected, check if close is within 0.50 points of a prior level
- On confirmation bar, require price to move beyond that level in the direction of the setup
- This creates a REAL filter that depends on bar history, not just current bar properties

This approach:
- Will produce DIFFERENT synthetic trades (level memory differs with random data)
- Tests the campaign hypothesis directly (absorption at carried levels)
- Should improve real data results by focusing on structural levels
- Keeps the confirmation structure that scored +0.1284

Expected: 350-450 trades on real data, DIFFERENT synthetic count, targeting +0.0400 to +0.0700.
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
        body_to_wick_ratio: float = 0.50,
        min_range_points: float = 0.75,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_memory_bars: int = 20,
        level_proximity: float = 0.50,
        level_granularity: float = 0.25,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.body_to_wick_ratio = body_to_wick_ratio
        self.min_range_points = min_range_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_memory_bars = level_memory_bars
        self.level_proximity = level_proximity
        self.level_granularity = level_granularity

        # Track price levels from recent bars (0.25-point granularity)
        self._level_memory: deque = deque(maxlen=level_memory_bars)
        
        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar: Optional[dict] = None
        self._pending_level: Optional[float] = None
        
        # Track bars we've entered on to avoid re-entry
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
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_level = None
            self._level_memory = deque(maxlen=self.level_memory_bars)

        # Update level memory with current bar's key prices
        self._update_level_memory(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_level = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_bar is not None:
            if self._should_confirm_setup(current, self._pending_setup, self._pending_level):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_level = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate at a carried level
        setup, level = self._detect_absorption(current)
        if setup:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_bar = {
                "high": current.high,
                "low": current.low,
                "open": current.open,
                "close": current.close,
                "range": current.high - current.low,
            }
            self._pending_level = level

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_level_memory(self, bar) -> None:
        """Add current bar's key prices to level memory."""
        # Round to granularity (0.25 points)
        high_rounded = round(bar.high / self.level_granularity) * self.level_granularity
        low_rounded = round(bar.low / self.level_granularity) * self.level_granularity
        close_rounded = round(bar.close / self.level_granularity) * self.level_granularity
        
        # Store unique levels
        levels = set()
        levels.add(high_rounded)
        levels.add(low_rounded)
        levels.add(close_rounded)
        
        for level in levels:
            self._level_memory.append(level)

    def _get_nearby_level(self, price: float) -> Optional[float]:
        """Find if there's a level in memory near this price."""
        if not self._level_memory:
            return None
        
        # Check all levels in memory for proximity
        for level in self._level_memory:
            if abs(price - level) <= self.level_proximity:
                return level
        
        return None

    def _detect_absorption(self, bar) -> tuple[Optional[dict], Optional[float]]:
        """
        Detect absorption bar at a carried level.
        Returns (setup dict, nearby_level) or (None, None)
        """
        if bar.volume == 0:
            return None, None

        delta = bar.delta
        delta_ratio = abs(delta) / bar.volume if bar.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None

        bar_range = bar.high - bar.low

        # Filter: Bar range must be meaningful
        if bar_range < self.min_range_points:
            return None, None

        # Check if close is near a carried level
        nearby_level = self._get_nearby_level(bar.close)
        if nearby_level is None:
            return None, None

        # LONG: Buying pressure with absorption at carried level
        if delta > 0:
            lower_wick = bar.close - bar.low
            
            # Filter: Lower wick must be substantial
            if lower_wick < self.min_wick_points:
                return None, None
            
            # Filter: Close must be in LOWER half of range
            mid_point = bar.low + bar_range / 2.0
            if bar.close > mid_point:
                return None, None
            
            # Filter: Body must be substantial relative to wick
            body_size = abs(bar.close - bar.open)
            if body_size < self.body_to_wick_ratio * lower_wick:
                return None, None
            
            # Calculate stop and target
            stop_price = bar.low - self.stop_buffer
            stop_distance = bar.close - stop_price
            
            # Filter: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, nearby_level
        
        # SHORT: Selling pressure with absorption at carried level
        elif delta < 0:
            upper_wick = bar.high - bar.close
            
            # Filter: Upper wick must be substantial
            if upper_wick < self.min_wick_points:
                return None, None
            
            # Filter: Close must be in UPPER half of range
            mid_point = bar.high - bar_range / 2.0
            if bar.close < mid_point:
                return None, None
            
            # Filter: Body must be substantial relative to wick
            body_size = abs(bar.close - bar.open)
            if body_size < self.body_to_wick_ratio * upper_wick:
                return None, None
            
            # Calculate stop and target
            stop_price = bar.high + self.stop_buffer
            stop_distance = stop_price - bar.close
            
            # Filter: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, nearby_level
        
        return None, None

    def _should_confirm_setup(self, confirming_bar, setup: dict, absorption_level: Optional[float]) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        Requirements:
        1. Price moves away from the absorption level
        2. Direction is correct (long moves above level, short moves below)
        
        This tests whether absorption at a carried level precedes a move away.
        """
        if absorption_level is None:
            return False
        
        if setup["direction"] == "long":
            # For long, confirming bar should close above the absorption level
            return confirming_bar.close > absorption_level
        else:  # short
            # For short, confirming bar should close below the absorption level
            return confirming_bar.close < absorption_level

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
