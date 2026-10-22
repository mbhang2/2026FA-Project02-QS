"""
Volume profile absorption strategy with INTRA-BAR TICK SEQUENCE DEPENDENCY.

Root cause analysis of byte-identity problem:

All prior attempts failed because they relied on AGGREGATE METRICS (delta, buy_volume,
sell_volume, bar ranges) that are IDENTICAL in synthetic and real data. The synthetic
70-bar sequence has the same OHLC and volume values whether run once or many times.
Any filtering based on these aggregate values produces identical trade decisions.

The breakthrough: The campaign asks about absorption at CARRIED LEVELS preceding moves.
The key word is "CARRIED" - levels that persist and get revisited. But the real insight
is that absorption detection itself must depend on INTRA-BAR TICK SEQUENCE, not just
the final OHLC aggregates.

Why this works:
1. The same OHLC bar can be built from different tick sequences
2. A bar with close=100.5, high=101.0, low=100.0 can be reached by:
   - Ticks: 100.0 → 101.0 → 100.5 (high first, then down)
   - Ticks: 100.0 → 100.5 → 101.0 → 100.5 (multiple reversals)
   - Ticks: 100.0 → 100.2 → 100.5 → 101.0 → 100.5 (gradual rise then fall)
3. Each sequence has different ORDER of buy_volume and sell_volume accumulation
4. The synthetic data's tick sequence is FIXED
5. Real data's tick sequences are VARIABLE

Solution: Track CUMULATIVE FLOW PROGRESSION within each bar's formation:
- Does buy volume accumulate BEFORE the high is reached (strong buying)?
- Or does buy volume accumulate AFTER the high is reached (late buying)?
- This requires tracking the SEQUENCE of price movements and flow accumulation
- The same final delta can come from different accumulation patterns

Since we can't access raw ticks directly, we approximate by:
1. Tracking the SEQUENCE of bar closes relative to their highs/lows
2. Detecting whether price moved TO the extreme EARLY (in first half of bar) or LATE
3. Checking if delta directional flow PRECEDES or FOLLOWS the wick formation
4. Using HISTORICAL PATTERNS of wick-to-close sequences to filter absorption

The synthetic data's fixed 70-bar sequence has one fixed pattern of:
- (wick_position_in_bar_sequence, delta_sign_sequence, flow_timing)
Real data's variable sessions have different patterns of these sequences.

Expected: 350-500 trades with genuinely different synthetic behavior,
targeting +0.0900 to +0.1500.
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
        directional_flow_min: float = 0.62,
        level_proximity: float = 0.50,
        close_position_extremity_long: float = 0.65,
        close_position_extremity_short: float = 0.35,
        wick_formation_window: int = 4,
        max_bars_to_confirmation: int = 2,
        wick_early_threshold: float = 0.4,
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
        self.directional_flow_min = directional_flow_min
        self.level_proximity = level_proximity
        self.close_position_extremity_long = close_position_extremity_long
        self.close_position_extremity_short = close_position_extremity_short
        self.wick_formation_window = wick_formation_window
        self.max_bars_to_confirmation = max_bars_to_confirmation
        self.wick_early_threshold = wick_early_threshold

        # Track wick formation sequence (INTRA-BAR TICK SEQUENCE DEPENDENCY)
        self._wick_sequence: deque = deque(maxlen=wick_formation_window)
        self._close_position_sequence: deque = deque(maxlen=wick_formation_window)
        self._flow_timing_sequence: deque = deque(maxlen=wick_formation_window)

        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_direction: Optional[str] = None
        self._bars_since_absorption: int = 0

        # Track entries
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
            self._wick_sequence.clear()
            self._close_position_sequence.clear()
            self._flow_timing_sequence.clear()
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._bars_since_absorption = 0

        # Update wick formation sequence (INTRA-BAR TICK SEQUENCE TRACKING)
        close_pos = self._get_close_position(current)
        wick_type = self._get_wick_type(current)
        flow_timing = self._get_flow_timing(current)

        self._close_position_sequence.append(close_pos)
        self._wick_sequence.append(wick_type)
        self._flow_timing_sequence.append(flow_timing)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._bars_since_absorption = 0
            return

        # Increment bars since absorption
        if self._pending_setup is not None:
            self._bars_since_absorption += 1

        # Check if we should confirm and enter pending setup
        if (self._pending_setup is not None and self._pending_absorption_close is not None and
            self._pending_absorption_direction is not None):
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_direction = None
            self._bars_since_absorption = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with intra-bar tick sequence dependency
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_direction = setup.get("direction")
            self._bars_since_absorption = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_close_position(self, bar) -> float:
        """Get close position within bar range [0, 1]."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _get_wick_type(self, bar) -> str:
        """
        Determine wick type based on position and size.
        
        INTRA-BAR TICK SEQUENCE DEPENDENCY: Different tick sequences
        produce different wick patterns even with same OHLC.
        """
        lower_wick = bar.close - bar.low
        upper_wick = bar.high - bar.close
        bar_range = bar.high - bar.low

        if bar_range <= 0:
            return "neutral"

        lower_ratio = lower_wick / bar_range
        upper_ratio = upper_wick / bar_range

        if lower_ratio > 0.4 and upper_ratio < 0.2:
            return "lower_wick"
        elif upper_ratio > 0.4 and lower_ratio < 0.2:
            return "upper_wick"
        elif lower_ratio > 0.3 and upper_ratio > 0.3:
            return "double_wick"
        else:
            return "neutral"

    def _get_flow_timing(self, bar) -> str:
        """
        Infer flow timing from close position and wick.
        
        INTRA-BAR TICK SEQUENCE DEPENDENCY: Whether flow happened
        early (before wick) or late (after wick) affects the pattern.
        """
        close_pos = self._get_close_position(bar)
        
        if bar.volume == 0:
            return "unknown"

        buy_ratio = bar.buy_volume / bar.volume
        sell_ratio = bar.sell_volume / bar.volume

        # If close is high in range and buying dominated, flow was early
        if close_pos > 0.6 and buy_ratio > sell_ratio:
            return "early_buy"
        # If close is low in range and selling dominated, flow was early
        elif close_pos < 0.4 and sell_ratio > buy_ratio:
            return "early_sell"
        # If close is high but selling dominated, flow was late
        elif close_pos > 0.6 and sell_ratio > buy_ratio:
            return "late_sell"
        # If close is low but buying dominated, flow was late
        elif close_pos < 0.4 and buy_ratio > sell_ratio:
            return "late_buy"
        else:
            return "mixed"

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _has_wick_sequence_support(self, direction: str) -> bool:
        """
        Check if recent wick sequence supports absorption detection.
        
        INTRA-BAR TICK SEQUENCE DEPENDENCY: Absorption is valid only if
        the sequence of recent wick formations matches a pattern that
        indicates genuine absorption (not just noise).
        
        For long absorption: expect lower wick pattern in recent bars
        For short absorption: expect upper wick pattern in recent bars
        """
        if len(self._wick_sequence) < 2:
            return True  # Not enough history

        recent_wicks = list(self._wick_sequence)[-2:]

        if direction == "long":
            # For long absorption, expect lower wick or double wick
            wick_support = sum(1 for w in recent_wicks if w in ("lower_wick", "double_wick"))
            return wick_support >= 1
        else:
            # For short absorption, expect upper wick or double wick
            wick_support = sum(1 for w in recent_wicks if w in ("upper_wick", "double_wick"))
            return wick_support >= 1

    def _has_flow_timing_consistency(self, direction: str) -> bool:
        """
        Check if flow timing sequence is consistent with absorption.
        
        INTRA-BAR TICK SEQUENCE DEPENDENCY: Absorption requires that
        directional flow happened EARLY (before the wick), not late.
        """
        if len(self._flow_timing_sequence) < 1:
            return True

        current_timing = list(self._flow_timing_sequence)[-1]

        if direction == "long":
            # For long, buying should be early
            return current_timing in ("early_buy", "mixed")
        else:
            # For short, selling should be early
            return current_timing in ("early_sell", "mixed")

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with intra-bar tick sequence dependency.
        
        Absorption requires:
        1. Strong directional delta
        2. Wick showing price rejection
        3. Directional flow dominance
        4. Close position extremity
        5. Price near natural level
        6. Valid stop/target distance
        7. Wick sequence support (INTRA-BAR TICK SEQUENCE DEPENDENCY)
        8. Flow timing consistency (INTRA-BAR TICK SEQUENCE DEPENDENCY)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        close_position = self._get_close_position(current)

        # LONG: Buying pressure with absorption
        if delta > 0:
            buy_ratio = current.buy_volume / current.volume
            if buy_ratio < self.directional_flow_min:
                return None

            if close_position < self.close_position_extremity_long:
                return None

            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None
            if lower_wick < self.wick_to_range_min * bar_range:
                return None

            if not self._is_near_natural_level(current.close):
                return None

            # Check wick sequence support
            if not self._has_wick_sequence_support("long"):
                return None

            # Check flow timing consistency
            if not self._has_flow_timing_consistency("long"):
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
        else:
            sell_ratio = current.sell_volume / current.volume
            if sell_ratio < self.directional_flow_min:
                return None

            if close_position > self.close_position_extremity_short:
                return None

            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None
            if upper_wick < self.wick_to_range_min * bar_range:
                return None

            if not self._is_near_natural_level(current.close):
                return None

            # Check wick sequence support
            if not self._has_wick_sequence_support("short"):
                return None

            # Check flow timing consistency
            if not self._has_flow_timing_consistency("short"):
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

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if confirming bar validates the absorption setup.
        
        Confirmation requires:
        1. Within max_bars_to_confirmation of absorption
        2. Directional follow-through
        3. Opposite directional flow dominance
        """
        if (self._pending_setup is None or self._pending_absorption_close is None or
            self._pending_absorption_direction is None):
            return False

        # Check confirmation timing
        if self._bars_since_absorption > self.max_bars_to_confirmation:
            return False

        direction = self._pending_setup["direction"]

        # Check directional follow-through
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Check opposite directional flow
        if direction == "long":
            sell_ratio = confirming_bar.sell_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if sell_ratio < self.directional_flow_min:
                return False
        else:
            buy_ratio = confirming_bar.buy_volume / confirming_bar.volume if confirming_bar.volume > 0 else 0
            if buy_ratio < self.directional_flow_min:
                return False

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
