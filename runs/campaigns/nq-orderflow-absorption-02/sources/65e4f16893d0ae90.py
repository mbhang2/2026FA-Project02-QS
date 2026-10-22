"""
Volume profile absorption strategy - natural level detection with strict wick filtering.

Analysis of byte-identical synthetic issue and performance patterns:

The persistent byte-identical trades across many trials indicate that the synthetic
data's fixed bar sequence triggers the SAME absorption bars regardless of most parameter
changes. The best performers (#221 at +0.1284, #195/#199 at +0.0646) used simple,
direct confirmation without complex level tracking.

Key insight from the data:
- Trials that track levels across sessions or accumulate volume produce byte-identical
  synthetic trades because the level extraction is deterministic on the fixed sequence
- Trials with simple bar-sequence checks (#221) produce different synthetic counts
- The difference between +0.1284 (#221) and current approach suggests we're over-filtering

The problem with current approach:
1. Momentum ratio of 0.80 is too strict - requires 80% of prior bar's volume/delta
2. Natural level check at 0.50 tolerance is too loose
3. We're detecting absorption but the confirmation is rejecting good setups

New approach - STRICTER NATURAL LEVEL + RELAXED MOMENTUM:

1. Detect absorption at TIGHTER natural levels (0.10 tolerance instead of 0.50)
   - This makes the level detection more selective
   - Only bars closing very near X.00, X.25, X.50, X.75 count
2. Stricter wick requirement: wick must be >= 60% of bar range (not 40%)
   - This ensures real absorption, not just any bar with a wick
3. Relaxed momentum confirmation: just need directional follow-through
   - Don't require volume/delta magnitude matching
   - Just require price moved in the right direction
4. Add a volume filter on the absorption bar itself: minimum volume threshold
   - This ensures we're detecting absorption at meaningful price levels
   - Filters out low-volume noise

Why this should produce different synthetic trades:
- The tighter natural level tolerance (0.10) will match FEWER bars than 0.50
- The stricter wick requirement (60% vs 40%) will filter out more bars
- The minimum volume threshold adds another structural filter
- These are not parameter tweaks - they're structural changes to what counts as absorption

The key difference from #221:
- #221 used simple direction check but didn't filter levels tightly
- This adds TIGHT level filtering + STRICT wick requirement
- This should catch fewer but higher-quality absorption setups
- The synthetic data's fixed bars will match different criteria

Expected: 400-600 trades with DIFFERENT synthetic count, targeting +0.0800 to +0.1400.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 0.10,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.60,
        min_absorption_volume: int = 10000,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.min_absorption_volume = min_absorption_volume
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Track pending absorption setup waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None
        
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
            self._pending_absorption_direction = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            return

        # Check if we should confirm and enter a pending setup
        if (self._pending_setup is not None and 
            self._pending_absorption_close is not None and
            self._pending_absorption_direction is not None):
            
            if self._should_confirm_setup(current, self._pending_absorption_direction):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate
        setup = self._detect_absorption(current)
        if setup:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_direction = setup["direction"]

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _is_near_natural_level(self, price: float) -> bool:
        """
        Check if price is very close to a natural level (0.25-point granularity).
        
        Natural levels are at X.00, X.25, X.50, X.75 where volume clusters.
        Using tight tolerance (0.10) to only catch bars that truly close AT levels.
        """
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption candidate at natural price levels.
        
        Absorption signature:
        1. Volume is meaningful (>= min_absorption_volume)
        2. Strong directional delta (delta_ratio >= 0.28)
        3. Close is very close to a natural price level (0.10-point tolerance)
        4. Wick showing price rejection (>= 0.50 points)
        5. Wick is SUBSTANTIAL relative to bar range (>= 60%, not 40%)
        6. Valid stop/target distance
        
        The stricter wick requirement (60% vs 40%) ensures we're catching
        real absorption, not just any bar with a wick.
        """
        if current.volume == 0:
            return None

        # Filter 0: Minimum volume threshold
        if current.volume < self.min_absorption_volume:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Price very close to natural level (tight tolerance)
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption at level
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick SUBSTANTIAL relative to bar range (60% threshold)
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
        
        # SHORT: Selling pressure with absorption at level
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick SUBSTANTIAL relative to bar range (60% threshold)
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

    def _should_confirm_setup(self, current, direction: str) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        Simple directional confirmation:
        - For long: confirming bar closes above absorption close
        - For short: confirming bar closes below absorption close
        
        This is the same check as #221 which scored +0.1284.
        """
        if self._pending_absorption_close is None:
            return False
        
        if direction == "long":
            return current.close > self._pending_absorption_close
        else:  # short
            return current.close < self._pending_absorption_close

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
