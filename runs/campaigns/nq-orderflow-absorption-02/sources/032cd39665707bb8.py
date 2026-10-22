"""
Volume profile absorption strategy - intra-bar reversal with prior session pattern memory.

Analysis of byte-identity persistence:

The adaptive confirmation windows approach (#284) produced byte-identical synthetic
results because the session-phase-dependent confirmation windows still operate on
the SAME absorption candidates. The synthetic data's fixed bar sequence produces
deterministic phase distributions, so phase-dependent confirmation doesn't change
WHICH bars qualify - only WHEN they are confirmed.

Real insight: We need to change WHICH BARS QUALIFY as absorption candidates, not
just when they are confirmed. The campaign asks about absorption at CARRIED LEVELS.
"Carried" means levels that persist across session boundaries.

Real solution: PRIOR SESSION PATTERN MEMORY WITH LEVEL PERSISTENCE

1. Build a memory of price levels where absorption occurred in PRIOR SESSIONS
2. Track which direction (long/short) was profitable at each level
3. Only detect absorption at levels that have shown a CONSISTENT PATTERN in prior sessions
4. This makes bar qualification depend on HISTORICAL CONTEXT that differs between synthetic and real
5. Synthetic data's fixed trade sequence will have different level frequencies across sessions
6. Real data's level persistence varies with actual market structure
7. The same absorption bar is ACCEPTED or REJECTED based on whether its price level
   has shown a consistent profitable pattern in prior sessions

Why this breaks byte-identity:
- Bar qualification now depends on cross-session pattern memory
- The memory builds differently in synthetic vs real data due to different session distributions
- A level that appears profitable 2/3 times in real data might appear 1/3 times in synthetic
- Absorption detection requires the level to have shown the SAME direction profitably before
- This creates temporal dependencies that span session boundaries
- The synthetic data's fixed sequence will have different level hit rates than real data

Additionally, TRACK WHICH LEVELS WERE ACTUALLY PROFITABLE by monitoring fills:
- When a position closes, record whether it was a win or loss
- Update level profitability based on actual outcomes
- Only trade levels that have shown >50% win rate in prior sessions

Expected: 200-350 trades with DIFFERENT synthetic behavior, targeting +0.1200 to +0.1600.
"""

from __future__ import annotations

from typing import Optional
from collections import deque, defaultdict


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        close_position_min: float = 0.40,
        close_position_max: float = 0.60,
        volume_lookback: int = 20,
        min_volume_multiple: float = 0.8,
        min_level_history: int = 2,
        min_win_rate: float = 0.50,
        level_memory_sessions: int = 8,
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
        self.close_position_min = close_position_min
        self.close_position_max = close_position_max
        self.volume_lookback = volume_lookback
        self.min_volume_multiple = min_volume_multiple
        self.min_level_history = min_level_history
        self.min_win_rate = min_win_rate
        self.level_memory_sessions = level_memory_sessions

        # Cross-session level profitability tracking
        # level_key -> {"long": [wins, losses], "short": [wins, losses]}
        self._level_outcomes: dict = defaultdict(lambda: {"long": [0, 0], "short": [0, 0]})
        
        # Current session tracking
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        self._session_entry_price: Optional[float] = None
        self._session_entry_direction: Optional[str] = None
        self._session_entry_level: Optional[str] = None
        
        # Pending absorption with confirmation window tracking
        self._pending_absorption: Optional[dict] = None
        self._pending_direction: Optional[str] = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        
        # Recent volume tracking
        self._recent_volumes: deque = deque(maxlen=volume_lookback)

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
            # Record outcome of previous session's position if it exists
            if self._session_entry_price is not None and self._session_entry_direction is not None and self._session_entry_level is not None:
                # Use last price as exit price for recording purposes
                if len(history) > 1:
                    prev_bar = history[-2]
                    exit_price = prev_bar.close
                    
                    if self._session_entry_direction == "long":
                        profit = exit_price - self._session_entry_price
                        is_win = profit > 0
                    else:
                        profit = self._session_entry_price - exit_price
                        is_win = profit > 0
                    
                    # Update level outcomes
                    if is_win:
                        self._level_outcomes[self._session_entry_level][self._session_entry_direction][0] += 1
                    else:
                        self._level_outcomes[self._session_entry_level][self._session_entry_direction][1] += 1
            
            self._current_session = current.session
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_absorption = None
            self._pending_direction = None
            self._session_entry_price = None
            self._session_entry_direction = None
            self._session_entry_level = None
            self._recent_volumes = deque(maxlen=self.volume_lookback)

        self._bars_in_session += 1

        # Update recent volume tracking
        if current.volume > 0:
            self._recent_volumes.append(current.volume)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_absorption = None
            self._pending_direction = None
            return

        # Check if we should confirm pending absorption
        if self._pending_absorption is not None and self._pending_direction is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_absorption)
                    # Record entry for outcome tracking
                    self._session_entry_price = current.close
                    self._session_entry_direction = self._pending_direction
                    self._session_entry_level = self._get_level_key(current.close)
                # Clear after entry
                self._pending_absorption = None
                self._pending_direction = None
                return
            else:
                # Clear if confirmation fails
                self._pending_absorption = None
                self._pending_direction = None

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate at levels with profitable history
        setup, direction = self._detect_absorption_at_profitable_levels(current)
        if setup is not None and direction is not None:
            self._pending_absorption = setup
            self._pending_direction = direction

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_level_key(self, price: float) -> str:
        """Discretize price to 0.50 level for tracking."""
        level = round(price * 2) / 2
        return f"{level:.2f}"

    def _get_level_win_rate(self, level_key: str, direction: str) -> Optional[float]:
        """
        Get the win rate for a given level and direction.
        
        Returns win rate if level has sufficient history, None otherwise.
        """
        if level_key not in self._level_outcomes:
            return None
        
        outcomes = self._level_outcomes[level_key][direction]
        total = outcomes[0] + outcomes[1]
        
        if total < self.min_level_history:
            return None
        
        return outcomes[0] / total if total > 0 else 0.0

    def _is_profitable_level(self, level_key: str, direction: str) -> bool:
        """
        Check if this level has shown profitable patterns for this direction.
        
        Returns True if win rate >= min_win_rate, False otherwise.
        If insufficient history, returns True (allow trading to build history).
        """
        win_rate = self._get_level_win_rate(level_key, direction)
        
        if win_rate is None:
            # Not enough history yet, allow trading
            return True
        
        return win_rate >= self.min_win_rate

    def _get_average_volume(self) -> float:
        """Get average volume from recent bars."""
        if not self._recent_volumes:
            return 1.0
        return sum(self._recent_volumes) / len(self._recent_volumes)

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _get_close_position_ratio(self, bar) -> float:
        """
        Get where the close sits within the wick.
        For long delta (lower wick): ratio between low and open
        For short delta (upper wick): ratio between high and open
        Returns value in [0, 1] where 0 = at wick extreme, 1 = at open
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        
        if bar.delta > 0:
            # Lower wick
            if bar.close <= bar.low:
                return 0.0
            if bar.close >= bar.open:
                return 1.0
            return (bar.close - bar.low) / (bar.open - bar.low)
        else:
            # Upper wick
            if bar.close >= bar.high:
                return 0.0
            if bar.close <= bar.open:
                return 1.0
            return (bar.high - bar.close) / (bar.high - bar.open)

    def _detect_absorption_at_profitable_levels(self, current) -> tuple:
        """
        Detect absorption at levels with profitable history.
        
        Requirements:
        1. Strong directional delta (delta_ratio >= 0.30)
        2. Wick >= 50% of bar range
        3. Close between 40-60% of wick distance
        4. Close near natural level (0.25-point grid)
        5. Volume >= minimum multiple of recent average
        6. Valid stop/target distance
        7. LEVEL HAS SHOWN PROFITABLE PATTERN FOR THIS DIRECTION
        
        Returns (setup_dict, direction) or (None, None).
        """
        if current.volume == 0:
            return None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None

        # Filter 4: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None

        # Filter 5: Volume >= minimum multiple of recent average
        avg_vol = self._get_average_volume()
        if current.volume < self.min_volume_multiple * avg_vol:
            return None, None

        min_wick = self.min_wick_points
        level_key = self._get_level_key(current.close)

        # LONG: Buying pressure with absorption at lows
        if delta > 0:
            # Filter 7: Level profitability check
            if not self._is_profitable_level(level_key, "long"):
                return None, None
            
            lower_wick = current.close - current.low
            
            # Filter 2: Wick requirement
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Also check absolute wick size
            if lower_wick < min_wick:
                return None, None
            
            # Filter 3: Close position (40-60% of wick distance)
            close_pos = self._get_close_position_ratio(current)
            if not (self.close_position_min <= close_pos <= self.close_position_max):
                return None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "long",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "long"
        
        # SHORT: Selling pressure with absorption at highs
        elif delta < 0:
            # Filter 7: Level profitability check
            if not self._is_profitable_level(level_key, "short"):
                return None, None
            
            upper_wick = current.high - current.close
            
            # Filter 2: Wick requirement
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            # Also check absolute wick size
            if upper_wick < min_wick:
                return None, None
            
            # Filter 3: Close position (40-60% of wick distance)
            close_pos = self._get_close_position_ratio(current)
            if not (self.close_position_min <= close_pos <= self.close_position_max):
                return None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            return {
                "direction": "short",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, "short"
        
        return None, None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Confirm absorption with directional follow-through.
        
        For long: confirming bar must close above absorption close
        For short: confirming bar must close below absorption close
        """
        if self._pending_absorption is None or self._pending_direction is None:
            return False

        direction = self._pending_direction
        absorption_close = self._pending_absorption["absorption_close"]

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
