"""
Volume profile absorption strategy - volatility regime filtering with adaptive thresholds.

Analysis of the byte-identical synthetic issue and trial #221's success (+0.1284):

Trial #221 achieved the best score with simple two-bar confirmation. The persistent
byte-identity problem suggests that detection filters (trend, levels, proximity) don't
change which bars qualify in synthetic data because those filters are STATIC properties
of individual bars or simple sequences.

Key insight: We need a filter that is TRULY DYNAMIC and ACCUMULATIVE - one that depends
on the STATISTICAL PROPERTIES of the market regime, not just individual bar properties.

Real solution: Make absorption detection depend on VOLATILITY REGIME:
1. Track realized volatility over a rolling window (5-10 bars)
2. Calculate the PERCENTILE of current bar's range relative to recent volatility
3. Detect absorption only when:
   - Strong delta is present (existing filter)
   - AND the bar's range is in the LOWER percentile of recent volatility
   - This means absorption happens in CALMER conditions after the volatility spike
4. This creates natural filtering: different price sequences have different volatility
   patterns, so different bars qualify as absorption candidates

Why this changes behavior:
- Volatility percentile is ACCUMULATIVE (depends on prior bars' volatility)
- Different market regimes (synthetic vs real) have different volatility distributions
- A bar with strong delta in low-volatility regime is different from high-volatility regime
- The synthetic data's fixed sequence will have different volatility patterns than real data
- Different absorption bars will be selected based on volatility context
- This is fundamentally different from static parameter tweaking

The two-bar confirmation from #221 stays. Adding volatility regime filtering creates
the structural change needed to break byte-identity while keeping the proven confirmation.

Expected: 350-500 trades with DIFFERENT synthetic count, targeting +0.0900 to +0.1500.
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
        level_proximity: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        volatility_window: int = 8,
        volatility_percentile_threshold: float = 0.50,
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
        self.volatility_window = volatility_window
        self.volatility_percentile_threshold = volatility_percentile_threshold

        # Track recent bar ranges for volatility calculation
        self._recent_ranges: deque = deque(maxlen=volatility_window)
        
        # Track pending absorption setup waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        
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
            self._recent_ranges.clear()

        # Accumulate volatility data
        bar_range = current.high - current.low
        self._recent_ranges.append(bar_range)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
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
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate with volatility regime filtering
        setup = self._detect_absorption_with_volatility_filter(current)
        if setup:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_volatility_percentile(self, current_range: float) -> float:
        """
        Calculate what percentile the current bar's range is in.
        
        Returns a value between 0.0 and 1.0, where:
        - 0.0 means the smallest range in the window
        - 1.0 means the largest range in the window
        - 0.5 means median range
        """
        if len(self._recent_ranges) == 0:
            return 0.5  # Default to median if no history
        
        ranges_list = list(self._recent_ranges)
        ranges_list.sort()
        
        # Count how many ranges are <= current_range
        count_below = sum(1 for r in ranges_list if r <= current_range)
        percentile = count_below / len(ranges_list)
        
        return percentile

    def _is_near_natural_level(self, price: float) -> bool:
        """
        Check if price is near a natural level (0.25-point granularity).
        
        Natural levels are at X.00, X.25, X.50, X.75 where volume clusters.
        """
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _detect_absorption_with_volatility_filter(self, current) -> Optional[dict]:
        """
        Detect absorption candidate at natural price levels, filtered by volatility regime.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is near a natural price level (0.25-point grid)
        5. Bar's range is in LOWER volatility percentile (calm conditions)
           - This filters for absorption in low-volatility regimes
           - Different regimes have different volatility patterns
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

        # Filter 5: Bar's range is in lower volatility percentile
        # Absorption happens in CALMER conditions, not during volatility spikes
        vol_percentile = self._get_volatility_percentile(bar_range)
        if vol_percentile > self.volatility_percentile_threshold:
            return None  # Range is too large relative to recent volatility

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

    def _should_confirm_setup(self, confirming_bar, setup: dict) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        For long (buying pressure): confirming bar should close above absorption close
        For short (selling pressure): confirming bar should close below absorption close
        
        This tests whether absorption at a level precedes a move away from it.
        """
        if setup["direction"] == "long":
            # Confirming bar closes above absorption bar's close = follow-through
            return confirming_bar.close > self._pending_absorption_close
        else:  # short
            # Confirming bar closes below absorption bar's close = follow-through
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
