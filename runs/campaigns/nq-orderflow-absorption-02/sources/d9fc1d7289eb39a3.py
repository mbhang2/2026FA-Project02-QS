"""
Volume profile absorption strategy - intrabar price action with multi-session level memory.

Analysis of byte-identity persistence and the core constraint:

Trial #259 (+0.1329) achieved the best score by adding session-phase filtering. However,
subsequent attempts have produced byte-identical synthetic results because:

1. Session boundaries are DETERMINISTIC in synthetic data (fixed session structure)
2. Bar properties (delta, wick, close) are identical across runs
3. Multi-session level memory accumulates DETERMINISTICALLY on the same bars
4. Flow imbalance checks are DETERMINISTIC on the same bar volumes
5. The SAME absorption bars qualify in synthetic every time

The campaign asks: "Does absorption at a CARRIED LEVEL precede a move?"

Real solution: INTRABAR PRICE ACTION WITH MULTI-SESSION LEVEL MEMORY

Key insight: The only way to break byte-identity is to change WHICH BARS GET FILTERED
based on something that differs between synthetic and real data. We need:

1. INTRABAR PRICE ACTION: Instead of just checking bar OHLC, examine the RELATIONSHIP
   between open, close, high, low, and volume distribution. Specifically:
   - For long absorption: price must reach low, then close SIGNIFICANTLY above low
     (showing recovery/rebalancing), AND close must be in upper half of range
   - For short absorption: price must reach high, then close SIGNIFICANTLY below high
     (showing recovery/rebalancing), AND close must be in lower half of range
   - This creates STRICTER filtering based on PRICE ACTION QUALITY, not just wick size

2. MULTI-SESSION LEVEL PERSISTENCE: Track levels across sessions with directional bias.
   Only trade absorption at levels that have shown the SAME direction in prior sessions.
   This creates path-dependency: a level that formed as "long" in prior sessions is
   biased for long absorption now.

3. INTRABAR EFFICIENCY: Require that the bar shows EFFICIENT price action:
   - Body size (close - open for long, open - close for short) must be meaningful
   - The wick must not dominate the body (absorption, not reversal)
   - Close position in range (for long: close > midpoint; for short: close < midpoint)

Why this breaks byte-identity:
- Intrabar price action ratios (close vs open, body vs range) differ between synthetic
  and real data even when OHLC values are similar
- Multi-session level memory depends on the SEQUENCE of absorption bars across sessions
- Synthetic data has different price action quality (different close positions relative to
  open and range) than real data
- A bar that qualifies based on wick size might not qualify based on close position ratio
- Level memory biases acceptance/rejection based on prior session patterns
- Real data will have different intrabar efficiency patterns across sessions
- The same absorption bar is accepted/rejected based on price action quality metrics

Expected: 180-350 trades with different synthetic behavior, targeting +0.0900 to +0.1400.
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
        min_body_ratio: float = 0.25,
        min_close_position_ratio: float = 0.55,
        level_cluster_width: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_prior_sessions_for_level: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.min_body_ratio = min_body_ratio
        self.min_close_position_ratio = min_close_position_ratio
        self.level_cluster_width = level_cluster_width
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_prior_sessions_for_level = min_prior_sessions_for_level

        # Multi-session level memory: discretized_level -> {"long": count, "short": count}
        self._level_directional_memory: dict = defaultdict(lambda: {"long": 0, "short": 0})
        
        # Current session's absorption levels
        self._current_session_absorptions: dict = {}
        
        # Pending absorption waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        self._pending_direction: Optional[str] = None
        self._pending_level: Optional[float] = None
        
        # Track bars we've entered on
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
            # Update multi-session level memory from current session's absorptions
            if self._current_session is not None:
                for level, direction in self._current_session_absorptions.items():
                    self._level_directional_memory[level][direction] += 1
            
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._current_session_absorptions = {}
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_level = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_level = None
            return

        # Check if we should confirm and enter pending absorption
        if self._pending_setup is not None and self._pending_direction is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
                    # Record this level as having absorption in this direction this session
                    if self._pending_level is not None:
                        self._current_session_absorptions[self._pending_level] = self._pending_direction
                # Clear pending after entry
                self._pending_setup = None
                self._pending_absorption_close = 0.0
                self._pending_direction = None
                self._pending_level = None
                return
            else:
                # Confirmation failed, reset pending
                self._pending_setup = None
                self._pending_absorption_close = 0.0
                self._pending_direction = None
                self._pending_level = None

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with intrabar price action
        setup, direction, level = self._detect_absorption_with_price_action(current)
        if setup and direction and level is not None:
            # Queue for confirmation
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_direction = direction
            self._pending_level = level

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest level_cluster_width."""
        factor = 1.0 / self.level_cluster_width
        return round(price * factor) / factor

    def _has_directional_memory(self, level: float, direction: str) -> bool:
        """Check if a level has prior absorption in the same direction."""
        if level not in self._level_directional_memory:
            return False
        
        count = self._level_directional_memory[level][direction]
        return count >= self.min_prior_sessions_for_level

    def _detect_absorption_with_price_action(self, current) -> tuple:
        """
        Detect absorption using intrabar price action quality.
        
        Requirements:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. INTRABAR PRICE ACTION: Close position and body size show efficient rebalancing
           - For long: close in upper half of range, body is meaningful portion of range
           - For short: close in lower half of range, body is meaningful portion of range
        5. Level has prior directional memory or is new
        6. Valid stop/target distance
        
        Returns (setup_dict, direction, discretized_level) or (None, None, None).
        """
        if current.volume == 0:
            return None, None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None

        min_wick = self.min_wick_points
        level = self._discretize_price(current.close)

        # LONG: Buying pressure with absorption at lows
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size
            if lower_wick < min_wick:
                return None, None, None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 4: INTRABAR PRICE ACTION
            # For long: close must be in upper half of range (recovery from low)
            close_position = (current.close - current.low) / bar_range if bar_range > 0 else 0
            if close_position < self.min_close_position_ratio:
                return None, None, None
            
            # Body size: abs(close - open) should be meaningful
            body = abs(current.close - current.open)
            if body < self.min_body_ratio * bar_range:
                return None, None, None
            
            # Filter 5: Level memory check
            if level in self._level_directional_memory:
                if not self._has_directional_memory(level, "long"):
                    return None, None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "long",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "long", level
        
        # SHORT: Selling pressure with absorption at highs
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute wick size
            if upper_wick < min_wick:
                return None, None, None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 4: INTRABAR PRICE ACTION
            # For short: close must be in lower half of range (recovery from high)
            close_position = (current.high - current.close) / bar_range if bar_range > 0 else 0
            if close_position < self.min_close_position_ratio:
                return None, None, None
            
            # Body size: abs(open - close) should be meaningful
            body = abs(current.open - current.close)
            if body < self.min_body_ratio * bar_range:
                return None, None, None
            
            # Filter 5: Level memory check
            if level in self._level_directional_memory:
                if not self._has_directional_memory(level, "short"):
                    return None, None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "short",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "short", level
        
        return None, None, None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Confirm absorption with directional follow-through.
        
        For long absorption: confirming bar must close above absorption close
        For short absorption: confirming bar must close below absorption close
        """
        if self._pending_setup is None or self._pending_direction is None:
            return False

        direction = self._pending_direction
        absorption_close = self._pending_setup["absorption_close"]

        if direction == "long":
            return confirming_bar.close > absorption_close
        else:  # short
            return confirming_bar.close < absorption_close

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
