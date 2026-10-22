"""
Volume profile absorption strategy - adaptive level detection with session-relative positioning.

Analysis of trial history and best performer (#221: +0.1284, 434 trades):

The byte-identical synthetic issue persists because all approaches so far detect
absorption based on ABSOLUTE bar properties (delta ratio, wick size, absolute price levels).
The synthetic data has deterministic bars, so these properties are fixed regardless of
parameter changes.

Key insight: Trial #221 succeeded with simple two-bar confirmation because it was
fundamentally different from the baseline - it DEFERRED entry to the next bar.
But the confirmation itself is still deterministic on bar properties.

Real solution: Make absorption detection depend on INTRA-SESSION PRICE CONTEXT that
accumulates and changes as the session progresses:

1. Track the session's price range (high/low so far) and volume distribution
2. Detect "relative extremes" - bars that close near the session's range extremes
3. Absorption at a relative extreme (price rejected from session high/low) is more
   meaningful than absorption at an arbitrary level
4. Add adaptive wick requirements based on recent volatility (not fixed thresholds)
5. This creates SESSION-DEPENDENT behavior: the same bar sequence looks different
   depending on where it sits in the session's overall price action

Why this changes behavior differently:
- Session high/low updates with every bar
- Volatility baseline updates with every bar
- A bar's qualification as "at a relative extreme" depends on session context
- The synthetic data's fixed sequence will have different session context than real data
- Different bars qualify as absorption candidates based on their session position
- This is fundamentally different from absolute-level detection

Additionally, only enter after PRICE REACHES the next natural level following absorption:
- Detect absorption at session relative extreme
- Wait for price to move to the next 0.25 level in that direction
- Only then enter
- This adds a structural price filter: not all absorption matters, only absorption
  that is followed by level-crossing move

Expected: 250-400 trades with DIFFERENT synthetic behavior, targeting +0.0800 to +0.1500.
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
        min_wick_points: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        volatility_window: int = 20,
        level_granularity: float = 0.25,
        relative_extreme_threshold: float = 0.75,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.volatility_window = volatility_window
        self.level_granularity = level_granularity
        self.relative_extreme_threshold = relative_extreme_threshold

        # Session-relative state
        self._session_high: float = 0.0
        self._session_low: float = 0.0
        self._session_open_price: float = 0.0
        
        # Volatility tracking (recent bar ranges)
        self._recent_ranges: deque = deque(maxlen=volatility_window)
        
        # Pending absorption waiting for level crossing
        self._pending_absorption: Optional[dict] = None
        self._pending_direction: Optional[str] = None
        self._target_level: Optional[float] = None
        
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

        # Handle session boundary - reset session-relative state
        if current.session != self._current_session:
            self._current_session = current.session
            self._session_high = current.high
            self._session_low = current.low
            self._session_open_price = current.open
            self._recent_ranges.clear()
            self._entered_bar_timestamps = set()
            self._pending_absorption = None
            self._pending_direction = None
            self._target_level = None
        else:
            # Update session high/low
            if current.high > self._session_high:
                self._session_high = current.high
            if current.low < self._session_low:
                self._session_low = current.low

        # Accumulate volatility data
        bar_range = current.high - current.low
        if bar_range > 0:
            self._recent_ranges.append(bar_range)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_absorption = None
            self._pending_direction = None
            self._target_level = None
            return

        # Check if pending absorption has reached its target level
        if self._pending_absorption is not None and self._target_level is not None:
            if self._has_crossed_target_level(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_absorption)
                self._pending_absorption = None
                self._pending_direction = None
                self._target_level = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at session relative extremes
        setup = self._detect_absorption_at_extreme(current)
        if setup is not None:
            direction = setup["direction"]
            target_level = self._calculate_target_level(current.close, direction)
            self._pending_absorption = setup
            self._pending_direction = direction
            self._target_level = target_level

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _quantize_level(self, price: float) -> float:
        """Quantize price to the level granularity."""
        return round(price / self.level_granularity) * self.level_granularity

    def _get_average_volatility(self) -> float:
        """Get the average of recent bar ranges."""
        if not self._recent_ranges:
            return self.min_wick_points
        return sum(self._recent_ranges) / len(self._recent_ranges)

    def _is_at_relative_extreme(self, close_price: float) -> bool:
        """
        Check if price is at a relative extreme within the session.
        
        A price is at a relative extreme if it's in the upper or lower
        threshold% of the session's range so far.
        """
        session_range = self._session_high - self._session_low
        if session_range <= 0:
            return False
        
        # Distance from session low as a percentage of range
        distance_from_low = close_price - self._session_low
        pct_from_low = distance_from_low / session_range
        
        # At relative extreme if in upper or lower threshold% of range
        is_near_high = pct_from_low > (1.0 - self.relative_extreme_threshold)
        is_near_low = pct_from_low < self.relative_extreme_threshold
        
        return is_near_high or is_near_low

    def _detect_absorption_at_extreme(self, current) -> Optional[dict]:
        """
        Detect absorption at session relative extremes.
        
        Absorption signature:
        1. Price closes at a relative extreme (near session high or low)
        2. Strong directional delta (delta_ratio >= 0.28)
        3. Wick showing price rejection from the extreme
        4. Wick size adaptive to recent volatility
        5. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will trigger entry when price crosses the next level.
        """
        if current.volume == 0:
            return None

        # Filter 1: Price at session relative extreme
        if not self._is_at_relative_extreme(current.close):
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 2: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Adaptive wick requirement based on recent volatility
        avg_volatility = self._get_average_volatility()
        adaptive_min_wick = max(self.min_wick_points, avg_volatility * 0.4)

        session_range = self._session_high - self._session_low
        if session_range <= 0:
            return None

        # LONG: Buying pressure at session low
        if delta > 0 and current.close < (self._session_low + session_range * self.relative_extreme_threshold):
            lower_wick = current.close - current.low
            
            # Filter 3: Adaptive wick size
            if lower_wick < adaptive_min_wick:
                return None
            
            # Filter 4: Wick relative to bar range
            if lower_wick < 0.35 * bar_range:
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
        
        # SHORT: Selling pressure at session high
        elif delta < 0 and current.close > (self._session_high - session_range * self.relative_extreme_threshold):
            upper_wick = current.high - current.close
            
            # Filter 3: Adaptive wick size
            if upper_wick < adaptive_min_wick:
                return None
            
            # Filter 4: Wick relative to bar range
            if upper_wick < 0.35 * bar_range:
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

    def _calculate_target_level(self, current_price: float, direction: str) -> float:
        """Calculate the next natural level in the direction of absorption."""
        quantized = self._quantize_level(current_price)
        if direction == "long":
            return quantized + self.level_granularity
        else:
            return quantized - self.level_granularity

    def _has_crossed_target_level(self, current) -> bool:
        """Check if price has crossed the target level."""
        if self._target_level is None:
            return False
        
        if self._pending_direction == "long":
            return current.close >= self._target_level
        else:  # short
            return current.close <= self._target_level

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on absorption with level crossing confirmation."""
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
