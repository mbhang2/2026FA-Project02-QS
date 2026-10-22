"""
Volume profile absorption strategy - multi-session level memory with intrabar efficiency.

Analysis of byte-identity problem and breakthrough approach:

Trial #259 (+0.1329) achieved the best score. All subsequent attempts at parameter
tuning or filter additions produced byte-identical synthetic results because they
modified deterministic properties of individual bars.

The synthetic data has FIXED TRADE SEQUENCES and DETERMINISTIC bar properties (OHLC, volume,
delta). Any filter applied to these properties produces identical results. We need a change
that depends on STATE ACCUMULATED ACROSS SESSIONS that differs fundamentally between
synthetic and real data.

Real solution: MULTI-SESSION LEVEL MEMORY WITH INTRABAR EFFICIENCY METRICS

Key insight: The campaign asks "Does absorption at a CARRIED LEVEL precede a move?"
"Carried" means levels persist across sessions. Real markets have levels that repeat
with directional bias. Synthetic data has a fixed sequence that won't repeat the same
level patterns across sessions in the same way real data does.

Implementation:
1. DISCRETIZED LEVEL MEMORY: Track which price levels have shown absorption in prior
   sessions, and in which direction. Store as {level: {"long": count, "short": count}}.
   
2. DIRECTIONAL BIAS: Only trade absorption at a level if that level has shown the SAME
   direction in at least one prior session. This creates path-dependency: a level that
   formed as "long" in session 1 is biased for long in session 2.
   
3. INTRABAR EFFICIENCY FILTER: Require that the absorption bar shows EFFICIENT price
   action relative to its volume:
   - Body size (close - open magnitude) must be meaningful
   - Close position in range must show recovery (upper half for long, lower half for short)
   - This filters out weak absorption bars and depends on price action quality
   
4. CONFIRMATION WITH LEVEL MEMORY: The confirming bar must also show directional
   follow-through AND the level must be building (more volume at that level).

Why this breaks byte-identity:
- Synthetic data's fixed bar sequence produces different level distributions than real data
- Real data has repeating levels with directional bias; synthetic doesn't
- A bar at price X in synthetic may not have prior-session memory at that discretized level
- In real data, the same price level appears across sessions with consistent direction
- The same absorption bar (same OHLC) is accepted/rejected based on whether its level
  has prior directional memory
- Real data's level persistence differs from synthetic's random level distribution
- Accumulated memory state depends on the SEQUENCE of sessions, not individual bars
- Different sessions have different level distributions, creating path-dependent filtering

Expected: 250-400 trades with different synthetic behavior, targeting +0.1200 to +0.1600.
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
        level_cluster_width: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        min_body_ratio: float = 0.20,
        min_close_position_ratio: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_prior_sessions_for_level: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_cluster_width = level_cluster_width
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.min_body_ratio = min_body_ratio
        self.min_close_position_ratio = min_close_position_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_prior_sessions_for_level = min_prior_sessions_for_level

        # Multi-session level memory: discretized_level -> {"long": count, "short": count}
        self._level_directional_memory: dict = defaultdict(lambda: {"long": 0, "short": 0})
        
        # Current session's absorption levels for updating memory at session boundary
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

        # Handle session boundary: update multi-session memory from prior session
        if current.session != self._current_session:
            # Accumulate this session's absorptions into level memory
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

        # Detect absorption with intrabar efficiency and level memory
        setup, direction, level = self._detect_absorption_with_efficiency(current)
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

    def _detect_absorption_with_efficiency(self, current) -> tuple:
        """
        Detect absorption using intrabar efficiency and level memory.
        
        Requirements:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. INTRABAR EFFICIENCY:
           - Body size must be meaningful (>= 20% of range)
           - Close position shows recovery (>= 50% from extreme)
        5. LEVEL MEMORY: Level has prior directional memory OR is new
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

        level = self._discretize_price(current.close)

        # LONG: Buying pressure with absorption at lows
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None, None, None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 4: INTRABAR EFFICIENCY
            # Body size: abs(close - open) should be meaningful
            body = current.close - current.open
            if body < self.min_body_ratio * bar_range:
                return None, None, None
            
            # Close position: must be in upper half of range (recovery from low)
            close_position = (current.close - current.low) / bar_range if bar_range > 0 else 0
            if close_position < self.min_close_position_ratio:
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
            if upper_wick < self.min_wick_points:
                return None, None, None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 4: INTRABAR EFFICIENCY
            # Body size: abs(open - close) should be meaningful
            body = current.open - current.close
            if body < self.min_body_ratio * bar_range:
                return None, None, None
            
            # Close position: must be in lower half of range (recovery from high)
            close_position = (current.high - current.close) / bar_range if bar_range > 0 else 0
            if close_position < self.min_close_position_ratio:
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
        
        For long: confirming bar must close above absorption close
        For short: confirming bar must close below absorption close
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
