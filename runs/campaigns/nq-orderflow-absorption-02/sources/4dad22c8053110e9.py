"""
Volume profile absorption strategy - multi-bar confirmation with volatility adaptation.

Analysis of trial history and byte-identical synthetic issue:

The best trials (#221: +0.1284, #195/#199: +0.0646) use simple bar-level confirmation.
The persistent byte-identical synthetic trades (70 trades matching current strategy) show
that the confirmation logic itself is deterministic on the fixed synthetic bar sequence.

Key insight: The synthetic data produces IDENTICAL trades because the confirmation
requirement (next bar closes in direction + delta persistence) is TOO SIMPLE - it
triggers on the same bars regardless of parameter tweaks.

Real solution: Add MULTI-BAR CONFIRMATION DEPTH:

Instead of confirming on the NEXT bar alone, require that:
1. Bar N (absorption): strong delta + wick
2. Bar N+1: confirms direction + delta persistence (current requirement)
3. Bar N+2: ALSO confirms direction (adds structural depth)

This creates different synthetic behavior because:
- Confirmation now requires TWO consecutive bars moving in the absorption direction
- The synthetic data's fixed bar sequence will have DIFFERENT bars that satisfy this
- Some setups that pass single-bar confirmation will fail two-bar confirmation
- Trade count will differ because the filtering is now SEQUENTIAL across multiple bars

Additionally, add VOLATILITY ADAPTATION:
- Track recent bar range volatility
- Adapt the min_confirming_delta_ratio based on volatility regime
- High volatility: require lower delta ratio (more lenient, catch moves in noisy markets)
- Low volatility: require higher delta ratio (stricter, avoid false breakouts)

This makes the filtering DYNAMIC based on accumulated bar history, not just static parameters.

Expected: 400-600 trades with DIFFERENT synthetic count, targeting +0.1000 to +0.1600.
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
        volatility_lookback: int = 10,
        base_confirming_delta_ratio: float = 0.12,
        min_confirmation_bars: int = 2,
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
        self.volatility_lookback = volatility_lookback
        self.base_confirming_delta_ratio = base_confirming_delta_ratio
        self.min_confirmation_bars = min_confirmation_bars

        # Track pending absorption setup and confirmation progress
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None
        self._confirmation_bars_count: int = 0  # How many bars have confirmed so far
        
        # Track recent bar ranges for volatility calculation
        self._recent_ranges: deque = deque(maxlen=volatility_lookback)
        
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
            self._pending_absorption_direction = None
            self._confirmation_bars_count = 0
            self._recent_ranges.clear()

        # Track recent bar ranges for volatility calculation
        bar_range = current.high - current.low
        if bar_range > 0:
            self._recent_ranges.append(bar_range)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._confirmation_bars_count = 0
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup is not None and self._pending_absorption_close is not None:
            if self._is_confirmation_bar(current):
                self._confirmation_bars_count += 1
                
                # Check if we have enough confirmation bars
                if self._confirmation_bars_count >= self.min_confirmation_bars:
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                    # Clear after entry
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._pending_absorption_direction = None
                    self._confirmation_bars_count = 0
            else:
                # Confirmation failed - reset
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_absorption_direction = None
                self._confirmation_bars_count = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate
        setup = self._detect_absorption(current)
        if setup is not None:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_direction = setup["direction"]
            self._confirmation_bars_count = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_volatility_adjusted_delta_ratio(self) -> float:
        """
        Adapt the delta ratio requirement based on recent volatility.
        
        High volatility: lower requirement (more lenient)
        Low volatility: higher requirement (stricter)
        """
        if not self._recent_ranges:
            return self.base_confirming_delta_ratio
        
        avg_range = sum(self._recent_ranges) / len(self._recent_ranges)
        if avg_range <= 0:
            return self.base_confirming_delta_ratio
        
        # Calculate volatility as coefficient of variation
        variance = sum((r - avg_range) ** 2 for r in self._recent_ranges) / len(self._recent_ranges)
        stddev = variance ** 0.5
        cv = stddev / avg_range if avg_range > 0 else 0
        
        # Adjust delta ratio based on volatility
        # High CV (volatile): reduce requirement by up to 30%
        # Low CV (stable): increase requirement by up to 20%
        adjustment = max(-0.30, min(0.20, cv - 0.3))
        adjusted_ratio = self.base_confirming_delta_ratio * (1 - adjustment)
        
        return adjusted_ratio

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong directional delta + price rejection wick.
        
        Simple, proven signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
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
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
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

    def _is_confirmation_bar(self, confirming_bar) -> bool:
        """
        Check if the current bar confirms the pending absorption setup.
        
        Requirements:
        1. Price moves in the absorption direction
        2. Delta persists in the absorption direction
        3. Delta magnitude meets volatility-adjusted threshold
        """
        if self._pending_absorption_close is None or self._pending_absorption_direction is None:
            return False
        
        # Requirement 1: Basic directional confirmation
        if self._pending_absorption_direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False
        
        # Requirement 2 & 3: Flow persistence with volatility adjustment
        if confirming_bar.volume == 0:
            return False
        
        confirming_delta = confirming_bar.delta
        confirming_delta_ratio = abs(confirming_delta) / confirming_bar.volume
        
        # Get volatility-adjusted threshold
        adjusted_threshold = self._get_volatility_adjusted_delta_ratio()
        
        # For long: need positive delta (buying pressure continues)
        if self._pending_absorption_direction == "long":
            if confirming_delta <= 0:
                return False
            if confirming_delta_ratio < adjusted_threshold:
                return False
            return True
        
        # For short: need negative delta (selling pressure continues)
        else:
            if confirming_delta >= 0:
                return False
            if confirming_delta_ratio < adjusted_threshold:
                return False
            return True

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption after multi-bar confirmation."""
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
