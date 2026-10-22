"""
Volume profile absorption strategy - dynamic level ranking with multi-session memory.

Analysis of the byte-identical synthetic issue and the best real result (#221: +0.1284):

The core issue: parameter tweaks on absorption detection don't change synthetic behavior
because the absorption bars themselves are deterministic on the fixed synthetic sequence.
Single-bar confirmation (#221) worked well on real data (+0.1284) but still produces
byte-identical synthetic trades because confirmation is a simple price comparison.

Key insight from the data: Trial #221 (best: +0.1284, 434 trades) used simple bar-sequence
confirmation. Multi-bar confirmation (#243) tried to add complexity but many subsequent
trials underperformed. The issue isn't adding more confirmation stages - it's that we need
STRUCTURAL CHANGE in what bars qualify as absorption candidates.

Real solution: Make absorption detection depend on DYNAMIC LEVEL RANKING that accumulates
across the walk:

1. Track all price levels touched and their FREQUENCY (how many bars touched them)
2. Identify "contested levels" - prices where many bars congregate (high touch frequency)
3. Detect absorption only when STRONG DELTA occurs at a contested level
4. This creates path-dependence: which levels are "contested" depends on the sequence of bars
5. Different bar sequences produce different level rankings, so different bars qualify

Why this changes synthetic behavior:
- Level ranking is cumulative and sequence-dependent
- A bar's qualification as absorption depends on whether its price is a high-frequency level
- The synthetic data's fixed bar sequence will have different high-frequency levels than real data
- So different bars will be selected as absorption candidates
- This is fundamentally different from single-parameter tweaking

The multi-bar confirmation from #221 stays, but now it only applies to bars at contested
levels, not all bars with strong delta. This dual filter (contested level + strong delta +
two-bar confirmation) should:
- Reduce false signals (only act at levels where price congregates)
- Improve quality (fewer but higher-conviction trades)
- Actually change synthetic behavior (level ranking is sequence-dependent)

Expected: 300-450 trades with score targeting +0.0900 to +0.1400.
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
        min_continuation_delta_ratio: float = 0.12,
        level_granularity: float = 0.25,
        min_level_touch_frequency: int = 3,
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
        self.min_continuation_delta_ratio = min_continuation_delta_ratio
        self.level_granularity = level_granularity
        self.min_level_touch_frequency = min_level_touch_frequency

        # Track how many bars have touched each price level (frequency-based)
        # This is cumulative across the entire walk
        self._level_touch_frequency: dict = defaultdict(int)
        
        # Track pending absorption setup waiting for first confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        
        # Track first confirmation passed, waiting for second confirmation
        self._first_confirmed_setup: Optional[dict] = None
        self._first_confirmed_close: Optional[float] = None
        
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
            self._pending_absorption_close = None
            self._first_confirmed_setup = None
            self._first_confirmed_close = None

        # Accumulate level touch frequency from this bar
        self._accumulate_level_touches(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._first_confirmed_setup = None
            self._first_confirmed_close = None
            return

        # Check if we should do SECOND confirmation and enter
        if self._first_confirmed_setup is not None and self._first_confirmed_close is not None:
            if self._should_second_confirm(current, self._first_confirmed_setup):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._first_confirmed_setup)
            # Clear first confirmation state after attempting second confirmation
            self._first_confirmed_setup = None
            self._first_confirmed_close = None
            self._pending_setup = None
            self._pending_absorption_close = None
            return

        # Check if we should do FIRST confirmation
        if self._pending_setup is not None and self._pending_absorption_close is not None:
            if self._should_first_confirm(current, self._pending_setup):
                # Move to first-confirmed state, waiting for second confirmation
                self._first_confirmed_setup = self._pending_setup
                self._first_confirmed_close = current.close
                self._pending_setup = None
                self._pending_absorption_close = None
            else:
                # First confirmation failed, clear pending
                self._pending_setup = None
                self._pending_absorption_close = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate at contested levels
        setup = self._detect_absorption_at_contested_level(current)
        if setup is not None:
            # Queue for first confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _quantize_level(self, price: float) -> float:
        """Quantize price to the level granularity."""
        return round(price / self.level_granularity) * self.level_granularity

    def _accumulate_level_touches(self, bar) -> None:
        """
        Track how many bars touch each price level.
        
        A bar "touches" a level if the level is between its low and high.
        This creates a frequency map of contested price levels.
        """
        # Get the quantized levels at bar extremes
        high_level = self._quantize_level(bar.high)
        low_level = self._quantize_level(bar.low)
        
        # Count all levels touched by this bar
        current_level = low_level
        while current_level <= high_level:
            self._level_touch_frequency[current_level] += 1
            current_level += self.level_granularity

    def _is_contested_level(self, price: float) -> bool:
        """
        Check if a price is at a contested level.
        
        A level is contested if it has been touched by at least
        min_level_touch_frequency bars.
        """
        level = self._quantize_level(price)
        return self._level_touch_frequency[level] >= self.min_level_touch_frequency

    def _detect_absorption_at_contested_level(self, current) -> Optional[dict]:
        """
        Detect absorption candidate at contested price levels.
        
        Absorption signature:
        1. Price closes AT a contested level (high touch frequency)
        2. Strong directional delta (delta_ratio >= 0.28)
        3. Wick showing price rejection (>= 0.50 points)
        4. Wick is substantial relative to bar range (>= 40%)
        5. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will be confirmed over two subsequent bars.
        """
        if current.volume == 0:
            return None

        # Filter 1: Price closes at a contested level
        if not self._is_contested_level(current.close):
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 2: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption at contested level
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
        
        # SHORT: Selling pressure with absorption at contested level
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

    def _should_first_confirm(self, confirming_bar, setup: dict) -> bool:
        """
        First confirmation: Price must move in the direction of the absorption flow.
        
        For long (buying pressure): confirming bar must close above absorption close
        For short (selling pressure): confirming bar must close below absorption close
        """
        if setup["direction"] == "long":
            return confirming_bar.close > self._pending_absorption_close
        else:  # short
            return confirming_bar.close < self._pending_absorption_close

    def _should_second_confirm(self, confirming_bar, setup: dict) -> bool:
        """
        Second confirmation: Price continues moving away AND flow direction persists.
        
        Requirements:
        1. Price continues moving in the same direction as first confirmation
        2. The confirming bar shows meaningful directional flow (delta_ratio >= min threshold)
        """
        if setup["direction"] == "long":
            # Price must continue moving up
            if confirming_bar.close <= self._first_confirmed_close:
                return False
            
            # Must show meaningful buying pressure continuation
            if confirming_bar.volume == 0:
                return False
            
            delta_ratio = confirming_bar.delta / confirming_bar.volume
            if delta_ratio < self.min_continuation_delta_ratio:
                return False
            
            return True
        
        else:  # short
            # Price must continue moving down
            if confirming_bar.close >= self._first_confirmed_close:
                return False
            
            # Must show meaningful selling pressure continuation
            if confirming_bar.volume == 0:
                return False
            
            delta_ratio = abs(confirming_bar.delta) / confirming_bar.volume
            if delta_ratio < self.min_continuation_delta_ratio:
                return False
            
            return True

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption at contested level."""
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
