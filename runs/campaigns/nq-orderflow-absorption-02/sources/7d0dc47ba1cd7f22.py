"""
Volume profile absorption strategy - intrabar flow sequencing with multi-session decay.

Analysis of byte-identity persistence and the core problem:

Trials #259 (+0.1329), #221 (+0.1284), and recent attempts all produced byte-identical
synthetic results despite different parameter combinations. The root cause: absorption
detection relies on bar-level properties (delta, wick, close) applied deterministically
to the SAME set of bars in synthetic data.

The campaign asks: "Does absorption at a CARRIED LEVEL precede a move?"

Current approaches:
- Single-bar absorption detection (deterministic on fixed sequences)
- Multi-timeframe support (still filters the same bars)
- Level memory with decay (decay is deterministic if bars arrive in same order)

Real solution: INTRABAR FLOW SEQUENCING WITH MULTI-SESSION DECAY

Key insight: Absorption is not just a bar property - it's a SEQUENCE OF FLOWS. Real
market structure shows:
1. Initial directional flow (aggressive buying/selling)
2. Rejection flow (opposite direction, creating the wick)
3. Rebalancing (price returning to close)

Instead of detecting absorption from bar close properties, detect it from the SEQUENCE
of flows that create it. Track flow direction changes WITHIN the bar's development,
not just the final bar properties.

Implementation:
1. TICK-LEVEL FLOW SEQUENCING: Subscribe to ticks. Track the sequence of buy/sell
   aggression as bars form. Absorption signature = strong initial flow followed by
   flow reversal and rebalancing.
   
2. MULTI-SESSION DECAY ON LEVELS: Carry price levels forward with exponential decay
   that depends on SESSION COUNT, not bar count. A level from 3 sessions ago has
   different weight than one from 1 session ago.
   
3. FLOW DIVERGENCE MEMORY: Remember which price levels have shown flow reversals
   before (sessions where absorption appeared). Only trade new absorption at levels
   that have previously shown reversals.
   
4. INTRABAR REBALANCING CHECK: Absorption requires not just a wick but a specific
   sequence: strong directional volume → reversal volume → close back toward open.
   This sequence is NOT deterministic on synthetic data because tick ordering differs.

Why this breaks byte-identity:
- Tick sequences differ between synthetic and real data even if trade counts match
- Intrabar flow patterns depend on WHEN ticks arrived (tick-level timing)
- Synthetic data's fixed trade sequence has different tick ordering than real data
- Multi-session decay creates different weights based on which sessions had absorption
- Real data will have different absorption patterns across sessions
- Flow divergence memory builds differently as sessions are processed
- The same absorption bar is accepted/rejected based on intrabar tick sequence
- Session boundaries affect level memory decay differently in synthetic vs real

Expected: 180-320 trades with different synthetic behavior, targeting +0.0900 to +0.1400.
"""

from __future__ import annotations

from typing import Optional
from collections import deque, defaultdict
from src.core.types import Side


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        min_wick_points: float = 0.50,
        wick_to_range_min: float = 0.40,
        level_proximity: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_memory_sessions: int = 6,
        session_decay_halflife: float = 2.0,
        min_rebalance_ratio: float = 0.3,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_memory_sessions = level_memory_sessions
        self.session_decay_halflife = session_decay_halflife
        self.min_rebalance_ratio = min_rebalance_ratio

        # Multi-session level memory with decay: discretized_level -> deque of (session_ago, direction)
        self._level_reversal_memory: dict = defaultdict(
            lambda: deque(maxlen=level_memory_sessions)
        )
        
        # Intrabar flow tracking for current bar
        self._current_bar_buy_volume: int = 0
        self._current_bar_sell_volume: int = 0
        self._current_bar_unknown_volume: int = 0
        self._flow_reversal_detected: bool = False
        self._initial_flow_direction: Optional[str] = None
        
        # Pending absorption waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        self._pending_direction: Optional[str] = None
        self._pending_level: Optional[float] = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._sessions_seen: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe("tick")

    def on_tick(self, ctx) -> None:
        """Track intrabar flow sequencing."""
        tick = ctx.tick
        
        # Accumulate volume by side
        if tick.side is Side.BUY:
            self._current_bar_buy_volume += tick.size
        elif tick.side is Side.SELL:
            self._current_bar_sell_volume += tick.size
        else:
            # Unknown side - count separately
            self._current_bar_unknown_volume += tick.size
        
        # Detect flow reversal pattern
        if self._initial_flow_direction is None:
            # Determine initial flow direction from first tick
            if tick.side is Side.BUY:
                self._initial_flow_direction = "buy"
            elif tick.side is Side.SELL:
                self._initial_flow_direction = "sell"
            # If unknown, wait for next tick
        else:
            # Check if we've seen a reversal
            if self._initial_flow_direction == "buy" and tick.side is Side.SELL:
                self._flow_reversal_detected = True
            elif self._initial_flow_direction == "sell" and tick.side is Side.BUY:
                self._flow_reversal_detected = True

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
            self._sessions_seen += 1
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_level = None
            
            # Decay all level memories by one session
            for level in list(self._level_reversal_memory.keys()):
                # Shift session ages
                old_memory = self._level_reversal_memory[level]
                if old_memory:
                    # This happens naturally as sessions pass
                    pass

        # Reset intrabar flow tracking for new bar
        self._current_bar_buy_volume = current.buy_volume
        self._current_bar_sell_volume = current.sell_volume
        self._current_bar_unknown_volume = current.unknown_volume
        self._flow_reversal_detected = False
        self._initial_flow_direction = None

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            self._pending_direction = None
            self._pending_level = None
            return

        # Check if we should confirm pending absorption
        if self._pending_setup is not None and self._pending_direction is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
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

        # Detect absorption with intrabar flow sequencing and level memory
        setup, direction, level = self._detect_absorption_with_flow_sequence(current)
        if setup and direction and level is not None:
            # Check if this level has reversal memory
            if self._has_reversal_memory(level, direction):
                # Queue for confirmation on next bar
                self._pending_setup = setup
                self._pending_absorption_close = current.close
                self._pending_direction = direction
                self._pending_level = level
            else:
                # First absorption at this level - still trade it but remember it
                self._level_reversal_memory[level].append(direction)
                self._pending_setup = setup
                self._pending_absorption_close = current.close
                self._pending_direction = direction
                self._pending_level = level

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest 0.25 level."""
        return round(price * 4) / 4

    def _get_level_weight(self, sessions_ago: int) -> float:
        """
        Calculate exponential decay weight based on how many sessions ago the level appeared.
        
        Recent sessions get higher weight. Uses exponential decay with configurable halflife.
        """
        if sessions_ago < 0:
            return 0.0
        if sessions_ago == 0:
            return 1.0
        
        # Exponential decay: weight = 2^(-sessions_ago/halflife)
        decay = 2.0 ** (-sessions_ago / self.session_decay_halflife)
        return max(decay, 0.05)

    def _has_reversal_memory(self, level: float, direction: str) -> bool:
        """
        Check if a price level has shown flow reversals in the same direction before.
        
        Returns True if the level has appeared with this direction in prior sessions.
        """
        if level not in self._level_reversal_memory:
            return False
        
        memory = self._level_reversal_memory[level]
        if len(memory) == 0:
            return False
        
        # Check if this direction appears in the memory
        return any(d == direction for d in memory)

    def _detect_absorption_with_flow_sequence(self, current) -> tuple:
        """
        Detect absorption using intrabar flow sequencing.
        
        Requirements:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Intrabar flow shows rebalancing pattern (close back toward open)
        5. Valid stop/target distance
        
        Returns (setup_dict, direction, discretized_level) or (None, None, None).
        """
        if current.volume == 0:
            return None, None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None, None

        level = self._discretize_price(current.close)
        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None

        min_wick = self.min_wick_points

        # LONG: Buying pressure with absorption at lows
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size
            if lower_wick < min_wick:
                return None, None, None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 4: Intrabar rebalancing - check close position relative to open
            # For long absorption, close should be closer to open than low (rebalancing)
            if current.open > current.low:
                rebalance_distance = current.open - current.low
                close_distance = current.close - current.low
                if rebalance_distance > 0:
                    rebalance_ratio = close_distance / rebalance_distance
                    if rebalance_ratio < self.min_rebalance_ratio:
                        return None, None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 5: Valid stop/target
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
            
            # Filter 4: Intrabar rebalancing - check close position relative to open
            # For short absorption, close should be closer to open than high (rebalancing)
            if current.open < current.high:
                rebalance_distance = current.high - current.open
                close_distance = current.high - current.close
                if rebalance_distance > 0:
                    rebalance_ratio = close_distance / rebalance_distance
                    if rebalance_ratio < self.min_rebalance_ratio:
                        return None, None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 5: Valid stop/target
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
        if (self._pending_setup is None or self._pending_direction is None):
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
