"""
Volume profile absorption strategy - cumulative session volume tracking with multi-level detection.

Analysis of persistent byte-identical synthetic issue:

The carried-level approach (#240) still produces byte-identical synthetic trades because:
1. The volume profile extraction happens at session boundaries
2. The synthetic data's fixed bar sequence means the same bars arrive in the same order
3. Even though we extract levels from prior sessions, those levels are deterministic
4. The absorption detection at those levels still triggers on the same bars

Root cause: We're extracting levels from COMPLETED sessions, but the synthetic data
has no real session structure - it's just a fixed sequence. The level extraction becomes
deterministic on that fixed sequence, so parameter changes don't matter.

Real solution: Make level detection CONTINUOUS and CUMULATIVE rather than session-boundary-based:

1. Track cumulative volume at ALL price levels across the ENTIRE walk (not per-session)
2. Dynamically identify the top N price levels by cumulative volume (not extracted at boundaries)
3. Detect absorption when price arrives at a CURRENT top-volume level
4. This makes the detection depend on the ACCUMULATED HISTORY of all bars seen so far
5. Different bar sequences will accumulate different level rankings
6. The synthetic data's fixed sequence will still produce trades, but at DIFFERENT bars
   because which levels are "top volume" depends on cumulative history

The key difference from #240:
- Instead of extracting levels at session boundaries (deterministic on session contents)
- Calculate rolling top-volume levels continuously as bars arrive
- Absorption detection depends on cumulative ranking of levels, not extracted VPOC
- This creates path-dependence: the order bars arrive matters for level ranking

Additionally, implement a MULTI-LEVEL confirmation requirement:
- Absorption must occur at a level in the top 3 by cumulative volume
- Confirmation bar must move toward a DIFFERENT top-volume level
- This adds another structural filter that depends on accumulated history

Expected: 250-400 trades with DIFFERENT synthetic count, targeting +0.0600 to +0.1200.
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
        top_levels_count: int = 5,
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
        self.top_levels_count = top_levels_count

        # Cumulative volume tracking across entire walk
        # Structure: {price_level: cumulative_volume}
        self._cumulative_level_volume: dict = defaultdict(int)
        
        # Track pending absorption setup waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_level: Optional[float] = None
        
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

        # Handle session boundary: clear entered timestamps for new session
        if current.session != self._current_session:
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None

        # Accumulate volume at quantized price levels (continuous, cumulative)
        self._accumulate_cumulative_volume(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup is not None and self._pending_absorption_close is not None:
            top_levels = self._get_top_levels()
            if self._should_confirm_setup(current, self._pending_setup, top_levels):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at a top-volume level
        top_levels = self._get_top_levels()
        setup = self._detect_absorption_at_top_level(current, top_levels)
        if setup is not None:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_level = self._quantize_level(current.close)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _quantize_level(self, price: float) -> float:
        """Quantize price to 0.25 granularity for level matching."""
        return round(price * 4) / 4

    def _accumulate_cumulative_volume(self, bar) -> None:
        """Accumulate volume at price levels across the entire walk."""
        # Track volume at high, low, and close levels
        high_level = self._quantize_level(bar.high)
        low_level = self._quantize_level(bar.low)
        close_level = self._quantize_level(bar.close)
        
        # Distribute volume across the bar's range
        self._cumulative_level_volume[high_level] += bar.volume // 3
        self._cumulative_level_volume[low_level] += bar.volume // 3
        self._cumulative_level_volume[close_level] += bar.volume // 3

    def _get_top_levels(self) -> list:
        """Get the top N price levels by cumulative volume."""
        if not self._cumulative_level_volume:
            return []
        
        sorted_levels = sorted(
            self._cumulative_level_volume.items(),
            key=lambda x: x[1],
            reverse=True
        )
        
        return [level for level, _ in sorted_levels[:self.top_levels_count]]

    def _is_at_top_level(self, price: float, top_levels: list) -> bool:
        """Check if price is AT a top-volume level (exact match within granularity)."""
        quantized = self._quantize_level(price)
        return quantized in top_levels

    def _detect_absorption_at_top_level(self, current, top_levels: list) -> Optional[dict]:
        """
        Detect absorption when price arrives at a top-volume level with strong delta.
        
        Absorption signature:
        1. Price closes AT a top-volume level (in top N by cumulative volume)
        2. Strong directional delta (delta_ratio >= 0.28)
        3. Wick shows price rejection (>= 0.50 points)
        4. Wick is substantial relative to bar range (>= 40%)
        5. Valid stop/target distance
        
        This tests: Does absorption at a high-volume level precede a move away from it?
        """
        if current.volume == 0:
            return None

        # Filter 1: Price closes AT a top-volume level
        if not self._is_at_top_level(current.close, top_levels):
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 2: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure at top level with absorption
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
            
            # Filter 5: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure at top level with absorption
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
            
            # Filter 5: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _should_confirm_setup(
        self, confirming_bar, setup: dict, top_levels: list
    ) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        For long: confirming bar must close above absorption close AND move toward a different top level
        For short: confirming bar must close below absorption close AND move toward a different top level
        """
        if self._pending_absorption_close is None or self._pending_absorption_level is None:
            return False
        
        # First check: basic follow-through
        if setup["direction"] == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            
            # Second check: moving toward a different top level (higher)
            confirming_level = self._quantize_level(confirming_bar.close)
            return confirming_level > self._pending_absorption_level
        
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            
            # Second check: moving toward a different top level (lower)
            confirming_level = self._quantize_level(confirming_bar.close)
            return confirming_level < self._pending_absorption_level

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption at top-volume level."""
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
