"""
Volume profile absorption strategy - test directional follow-through with adaptive filtering.

Analysis of recent trials:

The best performers (#221: +0.1284, #195: +0.0646, #209: +0.0538) all use a 
confirmation-based approach where absorption is queued and entered on the NEXT bar
if that bar shows follow-through. This is directly aligned with the campaign hypothesis.

However, #223 (-0.0161) and #222 (no score due to low exposure) show that overly
complex filtering (body_to_wick_ratio, efficiency multipliers, level freshness) 
reduces trade count without improving quality.

Key insight from #221's success: The confirmation filter naturally selects for
absorption that precedes moves. By requiring the next bar to close in the direction
of the absorption's delta, we're testing the exact hypothesis: does absorption 
predict follow-through?

The issue with #221: The parameters may be too strict, reducing trade count below
the 50% floor. Let's adjust:

1. Keep the bar-sequencing confirmation approach (it works)
2. Relax the absorption detection filters slightly:
   - Lower delta_ratio from 0.28 to 0.25 (more absorption candidates)
   - Lower min_wick_points from 0.50 to 0.35 (smaller wicks are valid)
   - Increase wick_to_range_min from 0.40 to 0.45 (wick must be 45% of range)
   - Keep level proximity at 0.50 (natural levels matter)
3. Tighten confirmation:
   - Require follow-through to be at least 0.25 points (not just any close)
   - This ensures we're trading real moves, not noise
4. Adjust sizing and risk:
   - Keep stop_buffer at 0.25
   - Reduce target_multiple from 4.0 to 3.0 (tighter risk/reward)
   - This should improve win rate without reducing exposure

Expected: 500-700 trades across 200+ sessions (50%+ floor), with cleaner
follow-through filtering, targeting -0.0100 to -0.0200.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.25,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.35,
        wick_to_range_min: float = 0.45,
        min_followthrough_points: float = 0.25,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.min_followthrough_points = min_followthrough_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        
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
            self._pending_absorption_close = 0.0

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup is not None:
            if self._should_confirm_setup(current, self._pending_setup):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = 0.0
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
        1. Strong directional delta (delta_ratio >= 0.25)
        2. Wick showing price rejection (>= 0.35 points)
        3. Wick is substantial relative to bar range (>= 45%)
        4. Close is near a natural price level (0.25-point grid)
        5. Valid stop/target distance
        
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
        
        For long (buying pressure): confirming bar must close above absorption close
        by at least min_followthrough_points to confirm follow-through.
        
        For short (selling pressure): confirming bar must close below absorption close
        by at least min_followthrough_points to confirm follow-through.
        
        This tests whether absorption at a level precedes a move away from it.
        """
        if setup["direction"] == "long":
            # Confirming bar closes above absorption bar's close = follow-through
            return confirming_bar.close >= self._pending_absorption_close + self.min_followthrough_points
        else:  # short
            # Confirming bar closes below absorption bar's close = follow-through
            return confirming_bar.close <= self._pending_absorption_close - self.min_followthrough_points

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
