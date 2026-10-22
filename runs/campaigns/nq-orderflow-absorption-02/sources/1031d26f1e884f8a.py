"""
Volume profile absorption strategy - focus on initiation logic to improve entry timing.

Analysis of prior attempts:
- #16 scored -0.1076 with 463 trades (pure absorption, no initiation)
- #13 scored -0.9744 with 102,546 trades (momentum fallback dominated)
- 47+ trials with identical synthetic trades suggests absorption detection is stable

Key insight: The campaign explicitly asks about initiation - whether waiting for a
confirming bar improves outcomes. #16's -0.1076 was pure absorption without waiting.

New strategy: Implement the TWO-SIGNAL approach the campaign requests:
1. **Absorption signal**: strong delta at a price level with wick opposite to flow
2. **Initiation signal**: a confirming bar showing efficient price movement relative
   to the flow behind it

The campaign states:
- "A rule that acts on absorption alone and a rule that waits for initiation are
  both answers"
- "Tag the entry with which it was" - so we can measure what waiting cost
- "Waiting is optional and it is the point"

Implementation:
- Detect absorption bars (strong delta + wick + at level)
- On the NEXT bar, check if it shows initiation (efficiency + body size)
- Place entries on BOTH absorption and initiation bars, with different tags
- This lets the record show which entry type performed better

Efficiency = points traveled per unit of signed flow
Body size = bar body as fraction of recent average range

This directly addresses the campaign's research question instead of trying to
guess what the baseline does.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.45,
        min_wick_ratio: float = 0.20,
        level_proximity: float = 0.75,
        efficiency_percentile: float = 0.75,
        min_body_ratio: float = 0.60,
        efficiency_window: int = 10,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_ratio = min_wick_ratio
        self.level_proximity = level_proximity
        self.efficiency_percentile = efficiency_percentile
        self.min_body_ratio = min_body_ratio
        self.efficiency_window = efficiency_window
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Current session tracking
        self._current_session: Optional[int] = None
        self._session_traded_prices: set[float] = set()
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        
        # Track absorption bars waiting for initiation
        self._pending_absorption: Optional[dict] = None
        self._pending_absorption_ts: Optional[int] = None
        
        # Efficiency tracking for initiation detection
        self._efficiency_history: deque = deque(maxlen=efficiency_window)
        self._range_history: deque = deque(maxlen=efficiency_window)

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
            self._on_session_change()
            self._current_session = current.session

        # Accumulate traded prices in current session
        self._accumulate_session_prices(current)
        
        # Track efficiency and range for initiation detection
        self._update_efficiency_metrics(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Check if pending absorption bar is followed by initiation
        if self._pending_absorption and self._pending_absorption_ts != current.close_ts:
            if self._is_initiating_bar(current):
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, self._pending_absorption, tag="absorption-initiated")
                self._pending_absorption = None
                self._pending_absorption_ts = None
            else:
                # Initiation didn't happen, clear pending
                self._pending_absorption = None
                self._pending_absorption_ts = None

        # Detect absorption signal on current bar
        absorption = self._detect_absorption(current)
        if absorption:
            # Option 1: Enter immediately on absorption
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption, tag="absorption-immediate")
            
            # Also store for potential initiation check on next bar
            self._pending_absorption = absorption
            self._pending_absorption_ts = current.close_ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Reset for new session."""
        self._session_traded_prices = set()
        self._entered_bar_timestamps = set()
        self._pending_absorption = None
        self._pending_absorption_ts = None
        self._efficiency_history.clear()
        self._range_history.clear()

    def _accumulate_session_prices(self, bar) -> None:
        """Track price levels that have been traded in this session."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Round to 0.25-point granularity
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        level = low_rounded
        while level <= high_rounded + 0.01:
            self._session_traded_prices.add(round(level * 4) / 4)
            level += 0.25

    def _update_efficiency_metrics(self, bar) -> None:
        """Track efficiency and range for initiation detection."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Efficiency = points traveled per unit of signed flow
        range_size = bar.high - bar.low
        if abs(bar.delta) > 0:
            efficiency = range_size / abs(bar.delta)
        else:
            efficiency = 0.0
        
        self._efficiency_history.append(efficiency)
        self._range_history.append(range_size)

    def _get_efficiency_threshold(self) -> float:
        """Get the efficiency threshold for initiation detection."""
        if not self._efficiency_history:
            return 0.0
        
        sorted_eff = sorted(self._efficiency_history)
        idx = int(len(sorted_eff) * self.efficiency_percentile)
        return sorted_eff[min(idx, len(sorted_eff) - 1)]

    def _get_avg_range(self) -> float:
        """Get average range for body size comparison."""
        if not self._range_history:
            return 0.0
        return sum(self._range_history) / len(self._range_history)

    def _is_initiating_bar(self, bar) -> bool:
        """
        Check if bar shows initiation: efficient price movement relative to flow,
        with body large relative to recent range.
        """
        if bar.volume <= 0 or bar.high <= bar.low:
            return False

        # Efficiency check: bar travels more per unit flow than recent average
        if abs(bar.delta) > 0:
            efficiency = (bar.high - bar.low) / abs(bar.delta)
        else:
            return False
        
        threshold = self._get_efficiency_threshold()
        if efficiency < threshold:
            return False

        # Body size check: bar body is large relative to recent range
        body_size = abs(bar.close - bar.open)
        avg_range = self._get_avg_range()
        
        if avg_range <= 0:
            return False
        
        body_ratio = body_size / avg_range
        if body_ratio < self.min_body_ratio:
            return False

        return True

    def _is_at_level(self, price: float) -> bool:
        """Check if price is at or very close to a session-traded level."""
        if not self._session_traded_prices:
            return False
        
        min_distance = min(abs(price - level) for level in self._session_traded_prices)
        return min_distance <= self.level_proximity

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong delta at a session-traded level with significant wick.
        
        Core requirements (ALL must be met):
        1. Price is at/near a level that was traded earlier in this session
        2. Strong one-sided flow (delta_ratio >= absorption_delta_ratio)
        3. Significant wick opposite to flow direction (>= min_wick_ratio of range)
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        # Check 1: Is price at a session-traded level?
        if not self._is_at_level(current.close):
            return None

        # Check 2: Strong one-sided flow
        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check 3: Significant wick opposite to flow
        range_size = current.high - current.low
        if range_size <= 0:
            return None

        has_significant_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick / range_size >= self.min_wick_ratio:
                has_significant_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick / range_size >= self.min_wick_ratio:
                has_significant_wick = True

        if not has_significant_wick:
            return None

        # Calculate stop and target
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # Short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

    def _place_entry(self, ctx, setup: dict, tag: str) -> None:
        """Place entry order on absorption detection."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
