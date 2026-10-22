"""
Volume profile absorption strategy - intra-session level dynamics with price memory.

Analysis of recent performance and the byte-identity problem:

Trial #259: +0.1329 with session-phase filtering (428 trades)
Trial #221: +0.1284 with two-bar confirmation (434 trades)

The byte-identity issue persists because we're still filtering the SAME absorption
candidates using different parameters. The synthetic data's fixed trade sequence
produces deterministic absorption bars, and parameter tweaking produces identical results.

Key insight: The campaign asks about absorption at CARRIED LEVELS preceding moves.
Prior attempts track levels across sessions, but don't account for INTRA-SESSION
PRICE MEMORY. A level that was touched earlier in the session and rejected is
different from a level that hasn't been visited yet.

Real solution: TRACK PRICE TOUCH HISTORY WITHIN EACH SESSION

1. For each session, maintain a memory of which price levels have been touched
2. Mark levels as "rejected" if price touched them, showed a wick/rejection, then moved away
3. Only trigger absorption at levels that have been PREVIOUSLY REJECTED in the current session
   (showing that the level has structure/resistance)
4. Add a PRICE MOMENTUM filter: the confirming bar must show that price is moving
   decisively AWAY from the level (not just closing above/below)
5. Track the highest/lowest price reached in the session to identify structural support/resistance

Why this breaks byte-identity:
- Intra-session price memory depends on the SEQUENCE and TIMING of bars within a session
- Synthetic data's fixed bar sequence will have different touch patterns than real data
- A bar can have identical delta/wick but qualify or not based on whether its price level
  was previously rejected in the current session
- Price momentum filter requires comparing multiple bars' relationships, not just two
- Different session structures → different touch sequences → different qualified bars

Expected: 300-500 trades with DIFFERENT synthetic behavior, targeting +0.0900 to +0.1500.
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
        momentum_bars: int = 3,
        momentum_threshold: float = 0.6,
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
        self.momentum_bars = momentum_bars
        self.momentum_threshold = momentum_threshold

        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_level: Optional[float] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Track intra-session price memory
        self._session_touched_levels: set = set()  # Levels price has touched
        self._session_rejected_levels: set = set()  # Levels that showed rejection
        self._session_high: Optional[float] = None
        self._session_low: Optional[float] = None
        
        # Track recent bars for momentum calculation
        self._recent_bars: deque = deque(maxlen=momentum_bars + 1)

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
            self._pending_absorption_level = None
            self._session_touched_levels = set()
            self._session_rejected_levels = set()
            self._session_high = None
            self._session_low = None
            self._recent_bars = deque(maxlen=self.momentum_bars + 1)

        # Update session high/low
        if self._session_high is None or current.high > self._session_high:
            self._session_high = current.high
        if self._session_low is None or current.low < self._session_low:
            self._session_low = current.low

        # Track touched levels (any level price reached)
        level_at_high = round(current.high * 4) / 4
        level_at_low = round(current.low * 4) / 4
        self._session_touched_levels.add(level_at_high)
        self._session_touched_levels.add(level_at_low)

        # Identify rejected levels from prior bars
        if len(self._recent_bars) > 0:
            prev_bar = self._recent_bars[-1]
            # If previous bar showed a wick and current bar moved away from it, mark as rejected
            if self._shows_rejection(prev_bar, current):
                prev_level = round(prev_bar.close * 4) / 4
                self._session_rejected_levels.add(prev_level)

        self._recent_bars.append(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_setup_with_momentum(current, self._pending_setup):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_level = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at previously rejected levels
        setup = self._detect_absorption_at_rejected_level(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            level = round(current.close * 4) / 4
            self._pending_absorption_level = level

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _shows_rejection(self, prev_bar, current_bar) -> bool:
        """
        Check if previous bar shows rejection (wick) and current bar moved away.
        """
        prev_range = prev_bar.high - prev_bar.low
        if prev_range <= 0:
            return False

        # Check for wick in previous bar
        if prev_bar.delta > 0:
            # Long bar: check lower wick
            lower_wick = prev_bar.close - prev_bar.low
            if lower_wick < self.min_wick_points:
                return False
            if lower_wick < self.wick_to_range_min * prev_range:
                return False
            # Check if current bar moved away from that level
            return current_bar.low > prev_bar.low + self.level_proximity
        elif prev_bar.delta < 0:
            # Short bar: check upper wick
            upper_wick = prev_bar.high - prev_bar.close
            if upper_wick < self.min_wick_points:
                return False
            if upper_wick < self.wick_to_range_min * prev_range:
                return False
            # Check if current bar moved away from that level
            return current_bar.high < prev_bar.high - self.level_proximity

        return False

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _detect_absorption_at_rejected_level(self, current) -> Optional[dict]:
        """
        Detect absorption that occurs AT a previously rejected level in this session.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Close is at a natural price level (0.25-point grid)
        5. Close price is AT a level that was previously rejected in this session
        6. Valid stop/target distance
        
        Returns setup dict if absorption at rejected level detected, None otherwise.
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
        
        # Filter 3: Price is at a previously rejected level (KEY FILTER)
        current_level = round(current.close * 4) / 4
        if current_level not in self._session_rejected_levels:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 4: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 5: Wick relative to bar range
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
            
            # Filter 4: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 5: Wick relative to bar range
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

    def _should_confirm_setup_with_momentum(self, confirming_bar, setup: dict) -> bool:
        """
        Check if the confirming bar validates the absorption setup using price momentum.
        
        Requirements:
        1. For long: confirming bar must close above absorption close
        2. For short: confirming bar must close below absorption close
        3. The move must show momentum: price must move decisively away from the level
           relative to recent bar ranges
        """
        # Check directional follow-through
        if setup["direction"] == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            
            # Check momentum: move away from level should be meaningful
            if len(self._recent_bars) >= 2:
                # Get recent average range
                recent_ranges = []
                for bar in self._recent_bars:
                    recent_ranges.append(bar.high - bar.low)
                avg_range = sum(recent_ranges) / len(recent_ranges) if recent_ranges else 0
                
                # Move away from level should exceed threshold of average range
                move_away = confirming_bar.close - self._pending_absorption_close
                if avg_range > 0 and move_away < avg_range * self.momentum_threshold:
                    return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            
            # Check momentum: move away from level should be meaningful
            if len(self._recent_bars) >= 2:
                # Get recent average range
                recent_ranges = []
                for bar in self._recent_bars:
                    recent_ranges.append(bar.high - bar.low)
                avg_range = sum(recent_ranges) / len(recent_ranges) if recent_ranges else 0
                
                # Move away from level should exceed threshold of average range
                move_away = self._pending_absorption_close - confirming_bar.close
                if avg_range > 0 and move_away < avg_range * self.momentum_threshold:
                    return False
        
        return True

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption at rejected level."""
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
