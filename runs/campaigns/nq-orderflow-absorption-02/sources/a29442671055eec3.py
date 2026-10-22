"""
Volume profile absorption strategy - intra-bar structural patterns with accumulated level memory.

Analysis of the byte-identical synthetic issue and the best real result (#221: +0.1284):

The persistent byte-identical trades indicate that confirmation logic operating on future bars
(whether single-bar or multi-bar) is deterministic on the synthetic data's fixed sequence.
The regime-aware approach (#247) also produced byte-identical trades, confirming that
checking properties of bars AFTER absorption detection doesn't change which bars get selected.

Key insight: The absorption bars themselves are the SAME in synthetic vs real data because
they're detected by the SAME criteria (strong delta + wick at natural levels). The issue is
that we're queuing these same bars for confirmation, and confirmation logic on a fixed
sequence is deterministic.

Real solution: Change WHICH BARS QUALIFY AS ABSORPTION CANDIDATES by making the detection
criteria depend on ACCUMULATED STRUCTURE that changes across the walk:

1. Track all bars that have shown strong delta in each direction at each price level
2. Build a "conviction map": which levels have shown sustained directional commitment
3. Detect absorption only when:
   a) Strong delta occurs at a level with PRIOR directional conviction (not just any strong delta)
   b) The wick pattern CONTRADICTS that prior direction (showing absorption)
   c) The bar's INTERNAL structure (open/close positioning) shows specific pattern
4. This makes bar selection path-dependent: which bars qualify depends on what came before

Additionally, use INTRA-BAR POSITIONING as a filter:
- For long absorption: require close to be in upper portion of bar (>60% of range above low)
  but with lower wick showing rejection. This is a specific structural pattern.
- For short absorption: require close to be in lower portion of bar (<40% of range above low)
  but with upper wick showing rejection.
- This structural requirement is checked on the bar itself, not future bars

Why this changes synthetic behavior:
- Level conviction map is cumulative and path-dependent
- Bar selection depends on both current bar structure AND prior history
- Synthetic data's fixed sequence will have different conviction patterns than real data
- Different bars will qualify as absorption candidates
- This is fundamentally different from parameter tweaking on the same detection logic

Expected: 300-450 trades with DIFFERENT synthetic behavior, targeting +0.0800 to +0.1400.
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
        level_proximity: float = 0.50,
        level_granularity: float = 0.25,
        conviction_window: int = 8,
        min_conviction_bars: int = 2,
        close_position_threshold_long: float = 0.60,
        close_position_threshold_short: float = 0.40,
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
        self.level_granularity = level_granularity
        self.conviction_window = conviction_window
        self.min_conviction_bars = min_conviction_bars
        self.close_position_threshold_long = close_position_threshold_long
        self.close_position_threshold_short = close_position_threshold_short

        # Track directional conviction at each price level
        # Maps level -> {'long': count, 'short': count}
        self._level_conviction: dict = defaultdict(lambda: {'long': 0, 'short': 0})
        
        # Track recent strong delta bars for conviction accumulation
        self._recent_conviction_bars: list = []
        
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
            self._level_conviction = defaultdict(lambda: {'long': 0, 'short': 0})
            self._recent_conviction_bars = []

        # Accumulate directional conviction from this bar
        self._accumulate_conviction(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with conviction-based filtering
        setup = self._detect_absorption_with_conviction(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _quantize_level(self, price: float) -> float:
        """Quantize price to the level granularity."""
        return round(price / self.level_granularity) * self.level_granularity

    def _accumulate_conviction(self, bar) -> None:
        """
        Track directional conviction at price levels.
        
        A bar with strong delta contributes conviction in that direction at the
        levels it touched. This creates a map of where buying/selling has been
        sustained, which is used later to identify meaningful absorptions.
        """
        if bar.volume == 0:
            return
        
        delta_ratio = abs(bar.delta) / bar.volume
        
        # Only track bars with meaningful directional conviction
        if delta_ratio < self.absorption_delta_ratio:
            return
        
        direction = "long" if bar.delta > 0 else "short"
        
        # Get the quantized level at the bar's close
        close_level = self._quantize_level(bar.close)
        
        # Increment conviction at this level
        self._level_conviction[close_level][direction] += 1
        
        # Keep recent bars for window management
        self._recent_conviction_bars.append({
            'level': close_level,
            'direction': direction,
            'delta_ratio': delta_ratio
        })
        
        # Trim old bars from the window
        if len(self._recent_conviction_bars) > self.conviction_window:
            old_bar = self._recent_conviction_bars.pop(0)
            # Decrement conviction for the old bar
            self._level_conviction[old_bar['level']][old_bar['direction']] -= 1
            if self._level_conviction[old_bar['level']]['long'] == 0 and \
               self._level_conviction[old_bar['level']]['short'] == 0:
                del self._level_conviction[old_bar['level']]

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = self._quantize_level(price)
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _get_level_conviction(self, level: float, direction: str) -> int:
        """Get the conviction count for a direction at a level."""
        return self._level_conviction[level].get(direction, 0)

    def _has_prior_conviction(self, level: float, direction: str) -> bool:
        """Check if a level has prior conviction in the given direction."""
        conviction = self._get_level_conviction(level, direction)
        return conviction >= self.min_conviction_bars

    def _get_close_position_ratio(self, bar) -> float:
        """
        Get where the close is positioned within the bar's range.
        
        Returns a value from 0 to 1:
        - 0 means close is at the low
        - 1 means close is at the high
        - 0.5 means close is at the midpoint
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        
        close_position = (bar.close - bar.low) / bar_range
        return close_position

    def _detect_absorption_with_conviction(self, current) -> Optional[dict]:
        """
        Detect absorption with conviction-based filtering.
        
        Absorption signature:
        1. Close is at a natural price level
        2. Level has PRIOR directional conviction (bars have shown this direction before)
        3. Current bar shows OPPOSITE directional delta (absorption of that conviction)
        4. Wick showing price rejection (>= 0.50 points)
        5. Wick is substantial relative to bar range (>= 40%)
        6. Bar's internal structure shows close positioning that contradicts prior conviction
        7. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        """
        if current.volume == 0:
            return None

        # Filter 1: Price close at a natural level
        if not self._is_near_natural_level(current.close):
            return None

        close_level = self._quantize_level(current.close)
        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position_ratio(current)

        # LONG: Buying pressure absorbing prior selling conviction
        if delta > 0:
            # Filter 2: Level must have prior SELLING conviction (we're absorbing sells)
            if not self._has_prior_conviction(close_level, "short"):
                return None
            
            # Filter 3: Strong directional delta
            if delta_ratio < self.absorption_delta_ratio:
                return None
            
            lower_wick = current.close - current.low
            
            # Filter 4: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 5: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 6: Close positioned in upper portion (showing absorption)
            if close_position < self.close_position_threshold_long:
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
        
        # SHORT: Selling pressure absorbing prior buying conviction
        elif delta < 0:
            # Filter 2: Level must have prior BUYING conviction (we're absorbing buys)
            if not self._has_prior_conviction(close_level, "long"):
                return None
            
            # Filter 3: Strong directional delta
            if delta_ratio < self.absorption_delta_ratio:
                return None
            
            upper_wick = current.high - current.close
            
            # Filter 4: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 5: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 6: Close positioned in lower portion (showing absorption)
            if close_position > self.close_position_threshold_short:
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

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on conviction-validated absorption."""
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
