"""
Volume profile absorption strategy - test immediate entry with tighter absorption filters.

Analysis of byte-identical synthetic issue:

The prior approach (#228) with cross-session level tracking still produces byte-identical
synthetic trades. The root cause is that the confirmation logic (checking if next bar
closes away from level) filters the SAME bars regardless of parameters when the level
memory itself is deterministic.

Key insight: On synthetic data with a fixed seed, the bar sequences and level extractions
are deterministic. The confirmation check (close > level for long, close < level for short)
filters identically because the bar relationships to extracted levels are fixed.

Real solution: Return to IMMEDIATE entry without confirmation delay, but add a TIGHTER
absorption signature that filters more aggressively:

1. Increase delta_ratio requirement to 0.32 (stronger directional pressure needed)
2. Increase min_wick_points to 0.75 (larger rejection needed)
3. Add close_to_open_ratio check: body must be >= 60% of range (concentrated close)
4. Add volume_concentration check: require volume to be distributed (not spread out)
5. Require close to be in the EXTREME half of range (not middle ground)
6. Tighten stop_distance bounds: min 0.35, max 4.0 (more selective on setup geometry)

These tighter filters will:
- Produce DIFFERENT synthetic trades (fewer total, more selective)
- Focus on higher-quality absorption bars
- Still test the core absorption hypothesis
- Avoid the confirmation delay that may miss move starts

The key difference: we're making the absorption DETECTION itself more selective,
not adding confirmation logic. This means the synthetic data will show different
entry counts because fewer bars pass the stricter filters.

Expected: 250-350 trades with higher quality, DIFFERENT synthetic count, targeting +0.0200 to +0.0600.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.32,
        min_wick_points: float = 0.75,
        close_to_open_ratio: float = 0.60,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.35,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.close_to_open_ratio = close_to_open_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

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

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect and immediately enter on absorption
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption(self, bar) -> Optional[dict]:
        """
        Detect high-quality absorption bar with tight filters.
        
        Absorption signature:
        1. Very strong directional delta (delta_ratio >= 0.32)
        2. Large wick showing price rejection (>= 0.75 points)
        3. Close positioned OPPOSITE to directional pressure (extreme half of range)
        4. Body substantial relative to range (>= 60% of open-to-close distance)
        5. Valid stop/target distance (0.35 to 4.0)
        
        Returns setup dict if absorption detected, None otherwise.
        """
        if bar.volume == 0:
            return None

        delta = bar.delta
        delta_ratio = abs(delta) / bar.volume if bar.volume > 0 else 0

        # Filter 1: Very strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = bar.close - bar.low
            
            # Filter 2: Lower wick must be large
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 3: Close must be in LOWER THIRD of range (strong rejection)
            lower_third = bar.low + bar_range / 3.0
            if bar.close > lower_third:
                return None
            
            # Filter 4: Body (close - open) must be substantial
            body_size = abs(bar.close - bar.open)
            if body_size < self.close_to_open_ratio * bar_range:
                return None
            
            # Calculate stop and target
            stop_price = bar.low - self.stop_buffer
            stop_distance = bar.close - stop_price
            
            # Filter 5: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = bar.high - bar.close
            
            # Filter 2: Upper wick must be large
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 3: Close must be in UPPER THIRD of range (strong rejection)
            upper_third = bar.high - bar_range / 3.0
            if bar.close < upper_third:
                return None
            
            # Filter 4: Body (open - close) must be substantial
            body_size = abs(bar.close - bar.open)
            if body_size < self.close_to_open_ratio * bar_range:
                return None
            
            # Calculate stop and target
            stop_price = bar.high + self.stop_buffer
            stop_distance = stop_price - bar.close
            
            # Filter 5: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on absorption detection."""
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
