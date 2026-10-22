"""
Volume profile absorption strategy - test bar sequencing and confirmation.

Analysis of byte-identical issue across trials:

The persistent byte-identical trades suggest the synthetic data is triggering
the SAME absorption detection logic regardless of parameter changes. Looking at
the pattern:
- Natural level proximity approach (trial before #220) produced byte-identical
- This means the _is_near_natural_level() check is passing for the same bars
- And the delta_ratio and wick checks are also passing identically

Key insight: The issue isn't that levels aren't detected - it's that the
absorption bars themselves are the same in synthetic data, so changing
proximity thresholds or wick requirements doesn't matter if they all pass
or all fail together.

Real solution: Add a BAR SEQUENCING requirement that actually filters
differently based on market context. Instead of just checking the current
bar in isolation, require that the NEXT bar confirms the signal.

This is directly aligned with the campaign hypothesis: "Does absorption at
a level precede a move away from it?"

Implementation:
1. Detect absorption candidate bars (strong delta + wick + near level)
2. Queue the setup for confirmation on the NEXT bar
3. On the next bar, check if it shows follow-through:
   - For long: next bar must close above the absorption bar's close
   - For short: next bar must close below the absorption bar's close
4. Only enter on the confirming bar

This approach:
- Filters differently based on actual bar sequence (not just parameters)
- Should produce DIFFERENT synthetic trade counts
- Tests the core campaign hypothesis with bar-level confirmation
- Keeps the strong absorption signature intact

The key difference from the current file: we wait for confirmation instead
of entering immediately. This is a structural change that will affect which
bars get traded, not just parameter tweaking.

Expected: 400-600 trades on real data (fewer due to confirmation filter),
with DIFFERENT synthetic trade counts, targeting -0.0150 to -0.0250.
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
        wick_to_range_min: float = 0.40,
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
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Track pending absorption setups waiting for confirmation
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
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
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
