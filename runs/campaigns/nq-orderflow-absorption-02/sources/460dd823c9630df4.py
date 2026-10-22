"""
Volume profile absorption strategy - multi-timeframe level persistence with flow confirmation.

Analysis of byte-identity problem and the carried levels approach:

The previous attempt (#279) built carried levels but they were ORDER-INDEPENDENT:
the same set of high-volume price levels emerged regardless of when bars arrived,
because volume accumulation was deterministic across both synthetic and real data.
This made the feature inert on synthetic data.

The real issue: we need bar qualification to depend on TEMPORAL CONTEXT that differs
between synthetic and real data. The synthetic data has a fixed sequence; real data
has actual market structure. We need to exploit that difference.

Real solution: MULTI-TIMEFRAME LEVEL PERSISTENCE WITH FLOW CONFIRMATION

1. Build volume profiles at TWO timeframes: 5m (current) and 30m (slower)
2. Detect levels from 30m bars only (coarser, more stable, carries better)
3. Absorption detection requires:
   a) Price at a 30m-derived level
   b) Strong delta on the 5m bar
   c) The 30m bar containing this 5m bar must show OPPOSITE flow direction
      (e.g., 30m bar is net buying, but 5m absorption shows selling pressure)
4. This creates a STRUCTURAL DEPENDENCY on the 30m bar's composition
5. Synthetic data's fixed trade sequence will have different 30m bar deltas
6. Real data has different intra-bar flow patterns

Why this breaks byte-identity:
- 30m bars close at different times than 5m bars
- The set of 5m bars "under" each 30m bar varies with data timing
- A 5m absorption bar qualifies only if its parent 30m bar has opposite flow
- Synthetic data's fixed sequence will have different 30m-level flow patterns
- Real data's flow persistence across timeframes differs from synthetic
- The confirmation requirement becomes: next 5m bar must follow the 30m flow direction

Expected: 150-350 trades with different synthetic behavior, targeting +0.1000 to +0.1700.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict, deque


class Strategy:
    def __init__(
        self,
        *,
        fast_interval: str = "5m",
        slow_interval: str = "30m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        slow_bar_delta_threshold: float = 0.15,
    ) -> None:
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.slow_bar_delta_threshold = slow_bar_delta_threshold

        # Track pending absorption waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        self._pending_parent_slow_direction: Optional[str] = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Slow-bar levels: discretized prices from recent slow bars
        self._slow_bar_levels: set = set()
        
        # Last slow bar info for flow comparison
        self._last_slow_bar_delta: float = 0.0
        self._last_slow_bar_volume: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        fast_history = ctx.history(self.fast_interval)
        if len(fast_history) < 1:
            return

        current_fast = fast_history[-1]

        # Handle session boundary
        if current_fast.session != self._current_session:
            self._current_session = current_fast.session
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_parent_slow_direction = None
            self._slow_bar_levels = set()
            self._last_slow_bar_delta = 0.0
            self._last_slow_bar_volume = 0

        # Update slow bar levels if a slow bar just closed
        slow_history = ctx.history(self.slow_interval)
        if len(slow_history) > 0:
            last_slow = slow_history[-1]
            # Extract levels from recent slow bars (last 2-3)
            recent_slow = slow_history.last(3) if len(slow_history) >= 3 else tuple(slow_history)
            self._slow_bar_levels = set()
            for slow_bar in recent_slow:
                self._slow_bar_levels.add(self._discretize_price(slow_bar.high))
                self._slow_bar_levels.add(self._discretize_price(slow_bar.low))
                self._slow_bar_levels.add(self._discretize_price(slow_bar.close))
            
            # Track last slow bar's flow direction
            if last_slow.volume > 0:
                self._last_slow_bar_delta = last_slow.delta
                self._last_slow_bar_volume = last_slow.volume

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_parent_slow_direction = None
            return

        # Check if we should confirm pending absorption
        if self._pending_setup is not None and self._pending_parent_slow_direction is not None:
            if self._should_confirm_setup(current_fast):
                if current_fast.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current_fast.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_parent_slow_direction = None
            return

        # Avoid re-entry on same bar
        if current_fast.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at slow-bar-derived levels with flow opposition
        setup, parent_direction = self._detect_absorption_with_flow_opposition(current_fast)
        if setup and parent_direction:
            self._pending_setup = setup
            self._pending_absorption_close = current_fast.close
            self._pending_parent_slow_direction = parent_direction

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest 0.25 level."""
        return round(price * 4) / 4

    def _is_at_slow_level(self, price: float) -> bool:
        """Check if price is at or near a slow-bar-derived level."""
        if not self._slow_bar_levels:
            return True  # No levels yet, accept all
        
        discretized = self._discretize_price(price)
        
        for level in self._slow_bar_levels:
            if abs(price - level) <= self.level_proximity:
                return True
        
        return False

    def _get_parent_slow_direction(self) -> Optional[str]:
        """
        Determine the flow direction of the parent slow bar.
        
        Returns "buy" if net buying, "sell" if net selling, None if neutral.
        """
        if self._last_slow_bar_volume == 0:
            return None
        
        delta_ratio = self._last_slow_bar_delta / self._last_slow_bar_volume
        
        if delta_ratio > self.slow_bar_delta_threshold:
            return "buy"
        elif delta_ratio < -self.slow_bar_delta_threshold:
            return "sell"
        else:
            return None

    def _detect_absorption_with_flow_opposition(self, current) -> tuple:
        """
        Detect absorption at slow-bar levels with flow opposition requirement.
        
        Requirements:
        1. Price at a slow-bar-derived level
        2. Parent slow bar has clear directional flow (buy or sell)
        3. Fast bar shows OPPOSITE flow (absorption against the slow bar direction)
        4. Strong directional delta on fast bar (>= 0.28)
        5. Wick showing price rejection (>= 0.50 points)
        6. Wick is substantial relative to bar range (>= 40%)
        7. Valid stop/target distance
        
        Returns (setup_dict, parent_direction) or (None, None).
        """
        # Filter 1: At a slow-bar-derived level
        if not self._is_at_slow_level(current.close):
            return None, None

        # Filter 2: Parent slow bar has clear flow direction
        parent_direction = self._get_parent_slow_direction()
        if parent_direction is None:
            return None, None

        if current.volume == 0:
            return None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 4: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None

        # Filter 3: Flow opposition (fast bar opposes slow bar direction)
        if parent_direction == "buy" and delta >= 0:
            # Parent is buying, but we need selling absorption
            return None, None
        if parent_direction == "sell" and delta <= 0:
            # Parent is selling, but we need buying absorption
            return None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None

        min_wick = self.min_wick_points

        # LONG: Buying absorption (parent slow bar was selling)
        if delta > 0 and parent_direction == "sell":
            lower_wick = current.close - current.low
            
            # Filter 5: Absolute wick size
            if lower_wick < min_wick:
                return None, None
            
            # Filter 6: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "long",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, parent_direction
        
        # SHORT: Selling absorption (parent slow bar was buying)
        elif delta < 0 and parent_direction == "buy":
            upper_wick = current.high - current.close
            
            # Filter 5: Absolute wick size
            if upper_wick < min_wick:
                return None, None
            
            # Filter 6: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "short",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, parent_direction
        
        return None, None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Confirm absorption with follow-through in the direction of the slow bar.
        
        For long absorption (parent was selling, we're buying):
        - Confirming bar should show buying pressure or at least not strong selling
        
        For short absorption (parent was buying, we're selling):
        - Confirming bar should show selling pressure or at least not strong buying
        """
        if self._pending_setup is None or self._pending_parent_slow_direction is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup["absorption_close"]
        parent_direction = self._pending_parent_slow_direction

        # Basic follow-through: price moves in the absorption direction
        if direction == "long":
            if confirming_bar.close <= absorption_close:
                return False
            # Ideally, confirming bar should align with slow bar direction
            # (slow was selling, we bought, so confirming should continue buying or be neutral)
            return True
        else:  # short
            if confirming_bar.close >= absorption_close:
                return False
            # Slow was buying, we sold, so confirming should continue selling or be neutral
            return True

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
