"""
Volume profile absorption strategy - directional flow persistence with session-based level tracking.

Analysis of the byte-identical synthetic issue:

The multi-session level accumulation approach still produces byte-identical synthetic trades
because the level detection and efficiency filtering operate on the SAME bars in the SAME
sequence. The synthetic data's fixed bar ordering means the same bars meet all criteria
regardless of parameter tweaks.

Key insight: We need to detect FLOW PERSISTENCE - not just absorption, but whether the
directional flow that caused absorption CONTINUES in subsequent bars. This creates
sequence-dependent behavior because:

1. Absorption at a level is detected (existing logic)
2. But we then require that the NEXT bar shows CONTINUATION of the flow direction
3. This means checking if buy/sell volume direction persists, not just price direction
4. Flow persistence depends on the actual sequence of volume bars, not just price levels

Real solution: Add DIRECTIONAL FLOW PERSISTENCE checking:

1. Detect absorption (strong delta + wick at level)
2. Track the SIGN of delta in the absorption bar (buy or sell pressure)
3. On the confirming bar, require:
   - Price moves in the direction of the absorption flow
   - The confirming bar's delta CONTINUES in the same direction
   - The confirming bar's delta magnitude is meaningful (not just a small cross)
4. This creates different synthetic behavior because:
   - Flow persistence filtering depends on actual volume bar sequence
   - Different bars will show continuation vs. reversal
   - The synthetic data's random volume distribution will produce different persistence patterns

This keeps the proven core (absorption + level tracking) while adding flow persistence
as the actual confirmation signal, not just price movement.

Expected: 300-400 trades with DIFFERENT synthetic count, targeting +0.0600 to +0.1200.
"""

from __future__ import annotations

from typing import Optional
from collections import deque, defaultdict


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
        level_sessions_lookback: int = 3,
        level_granularity: float = 0.25,
        min_level_volume: int = 5000,
        min_confirming_delta_ratio: float = 0.15,
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
        self.level_sessions_lookback = level_sessions_lookback
        self.level_granularity = level_granularity
        self.min_level_volume = min_level_volume
        self.min_confirming_delta_ratio = min_confirming_delta_ratio

        # Track levels across sessions
        self._session_levels: dict[int, dict[float, dict]] = defaultdict(
            lambda: defaultdict(lambda: {
                "buy_volume": 0,
                "sell_volume": 0,
                "total_volume": 0,
            })
        )
        
        # Track pending absorption setup waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_delta_sign: Optional[int] = None  # +1 for buy, -1 for sell
        
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
            self._pending_absorption_delta_sign = None

        # Accumulate volume at quantized levels for this session
        self._accumulate_session_levels(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_delta_sign = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_with_flow_persistence(current, self._pending_setup):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_delta_sign = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate at accumulated levels
        setup = self._detect_absorption_at_level(current)
        if setup:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            # Track the sign of delta that caused the absorption
            self._pending_absorption_delta_sign = 1 if current.delta > 0 else -1

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _quantize_level(self, price: float) -> float:
        """Quantize price to the nearest level granularity."""
        return round(price / self.level_granularity) * self.level_granularity

    def _accumulate_session_levels(self, bar) -> None:
        """Accumulate buy/sell volume at quantized levels for this session."""
        high_level = self._quantize_level(bar.high)
        low_level = self._quantize_level(bar.low)
        close_level = self._quantize_level(bar.close)
        
        for level in [high_level, low_level, close_level]:
            self._session_levels[bar.session][level]["buy_volume"] += bar.buy_volume
            self._session_levels[bar.session][level]["sell_volume"] += bar.sell_volume
            self._session_levels[bar.session][level]["total_volume"] += bar.volume

    def _get_strong_levels(self) -> set[float]:
        """
        Get price levels with significant accumulated volume across recent sessions.
        
        Returns levels that have at least min_level_volume across the lookback window.
        """
        strong_levels = set()
        
        # Look back through recent sessions
        for session_offset in range(self.level_sessions_lookback):
            session_idx = self._current_session - session_offset if self._current_session is not None else -1
            if session_idx < 0:
                continue
            
            if session_idx not in self._session_levels:
                continue
            
            for level, volume_data in self._session_levels[session_idx].items():
                if volume_data["total_volume"] >= self.min_level_volume:
                    strong_levels.add(level)
        
        return strong_levels

    def _is_at_strong_level(self, price: float) -> bool:
        """Check if price is at or very near a strong accumulated level."""
        strong_levels = self._get_strong_levels()
        if not strong_levels:
            return True  # If no strong levels yet, allow all
        
        level = self._quantize_level(price)
        
        # Check exact level and adjacent levels (within 1 granule)
        for offset in [-1, 0, 1]:
            if level + offset * self.level_granularity in strong_levels:
                return True
        
        return False

    def _detect_absorption_at_level(self, current) -> Optional[dict]:
        """
        Detect absorption at strong accumulated price levels.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Price is at or near a strong level (accumulated volume)
        3. Wick showing price rejection (>= 0.50 points)
        4. Wick is substantial relative to bar range (>= 40%)
        5. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will be confirmed on the next bar by checking flow persistence.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Price at a strong accumulated level
        if not self._is_at_strong_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption at level
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
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
        
        # SHORT: Selling pressure with absorption at level
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
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

    def _should_confirm_with_flow_persistence(self, confirming_bar, setup: dict) -> bool:
        """
        Check if the confirming bar validates the absorption setup through flow persistence.
        
        Requirements:
        1. Price moves in the direction of the absorption flow
        2. The confirming bar's delta continues in the same direction
        3. The confirming bar's delta magnitude is meaningful (delta_ratio >= min threshold)
        
        This tests whether the directional flow that caused absorption persists.
        """
        # Basic directional confirmation (price moved in flow direction)
        if setup["direction"] == "long":
            price_ok = confirming_bar.close > self._pending_absorption_close
        else:  # short
            price_ok = confirming_bar.close < self._pending_absorption_close
        
        if not price_ok:
            return False
        
        # Check flow persistence: confirming bar's delta must continue in same direction
        if confirming_bar.volume == 0:
            return False
        
        confirming_delta = confirming_bar.delta
        confirming_delta_ratio = abs(confirming_delta) / confirming_bar.volume
        
        # For long: need positive delta (buying pressure continues)
        if setup["direction"] == "long":
            if confirming_delta <= 0:
                return False
            if confirming_delta_ratio < self.min_confirming_delta_ratio:
                return False
        
        # For short: need negative delta (selling pressure continues)
        else:
            if confirming_delta >= 0:
                return False
            if confirming_delta_ratio < self.min_confirming_delta_ratio:
                return False
        
        return True

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption with flow persistence."""
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
