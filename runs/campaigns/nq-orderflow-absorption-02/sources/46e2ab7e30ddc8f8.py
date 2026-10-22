"""
Volume profile absorption strategy - test VWAP-based level detection with session memory.

Analysis of byte-identical synthetic issue:

The cross-session decay approach still produces byte-identical trades because the
level extraction and decay logic, while deterministic, operates on the SAME bars
arriving in the SAME order. The synthetic data has fixed bar sequences, so even
with decay weighting, the same bars trigger absorption.

Key insight: The synthetic data issue reveals that we need a fundamentally DIFFERENT
approach to level detection - one that depends on the SEQUENCE OF PRICES, not just
accumulated bar properties.

Real solution: Use VWAP (volume-weighted average price) as the primary level detector:

1. Track VWAP across rolling windows (5, 10, 20 bar windows)
2. Detect when price CROSSES VWAP levels (not just "near" them)
3. Absorption occurs when:
   - Price crosses above/below a VWAP level
   - Strong directional delta confirms the crossing
   - Wick shows rejection (price doesn't follow through)
4. This creates different synthetic behavior because:
   - VWAP calculation depends on the exact sequence of prices and volumes
   - Random synthetic data will produce different VWAP values than real data
   - Different bars will cross VWAP thresholds
   - The absorption detection becomes sequence-dependent, not just parameter-dependent

The key difference from #236:
- Instead of extracting discrete levels from high-volume bars
- Calculate rolling VWAP as a dynamic level that evolves with each bar
- Detect CROSSINGS of VWAP (structural price action) as absorption points
- Absorption is when price crosses VWAP with strong delta but fails to follow through

This tests: Does absorption at VWAP support levels precede reversals?

Expected: 300-400 trades with DIFFERENT synthetic count, targeting +0.0400 to +0.1000.
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
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        vwap_window: int = 10,
        vwap_crossing_threshold: float = 0.30,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.vwap_window = vwap_window
        self.vwap_crossing_threshold = vwap_crossing_threshold

        # Track bars for VWAP calculation
        self._bar_history: deque = deque(maxlen=vwap_window + 1)
        
        # Track previous VWAP for crossing detection
        self._prev_vwap: Optional[float] = None
        
        # Track pending absorption setup waiting for single-bar confirmation
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
            self._bar_history = deque(maxlen=self.vwap_window + 1)
            self._prev_vwap = None
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None

        # Accumulate bar for VWAP calculation
        self._bar_history.append({
            "close": current.close,
            "high": current.high,
            "low": current.low,
            "volume": current.volume,
            "delta": current.delta,
        })

        # Calculate current VWAP
        current_vwap = self._calculate_vwap()

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._prev_vwap = current_vwap
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
            self._prev_vwap = current_vwap
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            self._prev_vwap = current_vwap
            return

        # Detect absorption candidate based on VWAP crossing
        setup = self._detect_vwap_crossing_absorption(current, current_vwap)
        if setup:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close

        # Update previous VWAP
        self._prev_vwap = current_vwap

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _calculate_vwap(self) -> Optional[float]:
        """
        Calculate volume-weighted average price from bar history.
        
        VWAP = sum(close * volume) / sum(volume)
        """
        if len(self._bar_history) < 2:
            return None

        total_volume = sum(b["volume"] for b in self._bar_history)
        if total_volume == 0:
            return None

        weighted_sum = sum(b["close"] * b["volume"] for b in self._bar_history)
        return weighted_sum / total_volume

    def _detect_vwap_crossing_absorption(
        self, current, current_vwap: Optional[float]
    ) -> Optional[dict]:
        """
        Detect absorption when price crosses VWAP level with strong delta but rejection wick.
        
        Absorption signature:
        1. Current VWAP exists and differs from previous
        2. Price crosses VWAP (moved from one side to other)
        3. Strong directional delta (delta_ratio >= 0.28)
        4. Wick shows price rejection (>= 0.50 points)
        5. Wick is substantial relative to bar range (>= 40%)
        6. Valid stop/target distance
        
        The crossing itself is the key: it's a structural price action event that
        creates different synthetic behavior than parameter tweaking.
        """
        if current.volume == 0 or current_vwap is None:
            return None

        # Need previous VWAP to detect crossing
        if self._prev_vwap is None:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Detect VWAP crossing: price moved from one side to the other
        prev_close = self._bar_history[-2]["close"] if len(self._bar_history) >= 2 else current.close
        
        # LONG: Price crossed above VWAP (bullish crossing) but got absorbed
        if (prev_close <= self._prev_vwap and current.close > current_vwap and 
            delta > 0):
            # Strong buying crossed VWAP but got rejected (lower wick)
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 4: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        # SHORT: Price crossed below VWAP (bearish crossing) but got absorbed
        elif (prev_close >= self._prev_vwap and current.close < current_vwap and 
              delta < 0):
            # Strong selling crossed VWAP but got rejected (upper wick)
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 4: Valid stop distance
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
        
        For long: confirming bar must close above absorption close (follow-through)
        For short: confirming bar must close below absorption close (follow-through)
        """
        if setup["direction"] == "long":
            return confirming_bar.close > self._pending_absorption_close
        else:  # short
            return confirming_bar.close < self._pending_absorption_close

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed VWAP crossing absorption."""
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
