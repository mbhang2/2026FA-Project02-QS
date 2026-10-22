"""
Volume profile absorption strategy - refine detection with initiation confirmation.

Analysis of trial #16 (best real score: -0.1076, 463 trades):
- Simple absorption detection: strong delta + wick opposite to flow
- No level proximity check
- Immediate entry on absorption bar
- Synthetic: 70 trades (identical to baseline)

The campaign question asks: Does absorption precede a move, and does waiting for
initiation help or hurt?

Key insight from the question: A confirming bar that moves price efficiently
relative to flow (high efficiency = points traveled per unit signed flow) may
improve entries by filtering absorption that leads nowhere.

New approach:
1. **Detect absorption** - strong delta + wick opposite to flow (same as #16)
2. **Wait for initiation** - next bar after absorption must show:
   - Efficiency above recent rolling average (points per unit signed flow)
   - Body size large relative to recent range
   - This tags the entry as "absorption+initiation" vs just "absorption"
3. **Track both paths separately** - enter on absorption alone OR after initiation
4. **Report the difference** - tag reveals which rule fired

Why this might improve on #16:
- #16 entered immediately on absorption (463 trades, -0.1076 score)
- Adding initiation filter creates two entry types:
  - Absorption-only: captures immediate reversals
  - Absorption+initiation: filters for confirmed moves
- The tag makes the comparison readable in the run record
- If initiation improves the score, we learn that waiting helps
- If it doesn't, we learn that absorption alone is better

Implementation:
- Calculate bar efficiency: signed_flow_points / abs(signed_flow)
- Track rolling average efficiency (last 20 bars, separately for buy/sell flow)
- Initiation bar must have efficiency > 75th percentile of recent history
- Initiation bar must have body > 50% of recent average range
- Enter on absorption bar alone, OR wait for initiation bar
- Both entries use same structural stop (beyond bar extreme + buffer)
- Both entries use same target multiple (3.0x stop distance)

The trade-off: initiation will reduce trade count but may improve quality.
If score improves despite fewer trades, initiation helps.
If score worsens, absorption alone is better.
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        min_wick_ratio: float = 0.08,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        efficiency_window: int = 20,
        efficiency_threshold: float = 0.75,
        min_body_ratio: float = 0.50,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.efficiency_window = efficiency_window
        self.efficiency_threshold = efficiency_threshold
        self.min_body_ratio = min_body_ratio
        self.size = size

        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Track absorption bars waiting for initiation
        self._pending_absorption: Optional[dict] = None
        
        # Track efficiency history for buy and sell flow separately
        self._buy_efficiency_history: deque = deque(maxlen=efficiency_window)
        self._sell_efficiency_history: deque = deque(maxlen=efficiency_window)
        
        # Track recent range for body size comparison
        self._recent_ranges: deque = deque(maxlen=efficiency_window)

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
            self._pending_absorption = None
            self._buy_efficiency_history = deque(maxlen=self.efficiency_window)
            self._sell_efficiency_history = deque(maxlen=self.efficiency_window)
            self._recent_ranges = deque(maxlen=self.efficiency_window)

        # Update efficiency and range tracking
        self._update_efficiency_tracking(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_absorption = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Check if pending absorption gets confirmed by initiation
        if self._pending_absorption is not None:
            if self._check_initiation(current):
                self._entered_bar_timestamps.add(self._pending_absorption["ts"])
                self._place_entry(ctx, self._pending_absorption, tag_suffix="initiation")
            self._pending_absorption = None

        # Detect new absorption
        absorption = self._detect_absorption(current)
        if absorption is not None:
            # Option 1: Enter immediately on absorption
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption, tag_suffix="alone")
            
            # Option 2: Also set pending for potential initiation confirmation
            # (but only if we have room for both - action cap is 64)
            # For now, just track it for next bar
            self._pending_absorption = {
                "direction": absorption["direction"],
                "stop_distance": absorption["stop_distance"],
                "target_distance": absorption["target_distance"],
                "ts": current.close_ts,
            }

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_efficiency_tracking(self, bar) -> None:
        """Track bar efficiency and recent range for initiation detection."""
        if bar.volume == 0:
            return
        
        # Track range
        range_size = bar.high - bar.low
        if range_size > 0:
            self._recent_ranges.append(range_size)
        
        # Calculate efficiency: price movement per unit of signed flow
        signed_flow = bar.delta  # buy_volume - sell_volume
        if signed_flow == 0:
            return
        
        # Points traveled: use close-to-close movement or range traversed
        # For efficiency, use the range traversed
        efficiency = range_size / abs(signed_flow) if signed_flow != 0 else 0
        
        if signed_flow > 0:
            self._buy_efficiency_history.append(efficiency)
        else:
            self._sell_efficiency_history.append(efficiency)

    def _get_efficiency_threshold(self, direction: str) -> Optional[float]:
        """Get efficiency threshold for initiation (75th percentile of recent history)."""
        if direction == "long":
            history = list(self._buy_efficiency_history)
        else:
            history = list(self._sell_efficiency_history)
        
        if len(history) < 3:
            return None
        
        sorted_hist = sorted(history)
        idx = int(len(sorted_hist) * self.efficiency_threshold)
        return sorted_hist[min(idx, len(sorted_hist) - 1)]

    def _get_recent_avg_range(self) -> Optional[float]:
        """Get average range of recent bars."""
        if len(self._recent_ranges) < 3:
            return None
        return sum(self._recent_ranges) / len(self._recent_ranges)

    def _check_initiation(self, current) -> bool:
        """
        Check if current bar confirms pending absorption as initiation.
        
        Initiation requires:
        1. Efficiency above recent 75th percentile (good price movement per flow)
        2. Body size > 50% of recent average range (strong move)
        3. Flow in same direction as absorption
        """
        if self._pending_absorption is None:
            return False
        
        direction = self._pending_absorption["direction"]
        
        # Get thresholds
        eff_threshold = self._get_efficiency_threshold(direction)
        avg_range = self._get_recent_avg_range()
        
        if eff_threshold is None or avg_range is None or avg_range <= 0:
            return False
        
        # Calculate current bar's efficiency
        if current.volume == 0 or current.delta == 0:
            return False
        
        range_size = current.high - current.low
        if range_size <= 0:
            return False
        
        efficiency = range_size / abs(current.delta)
        
        # Check efficiency threshold
        if efficiency < eff_threshold:
            return False
        
        # Check body size (body = abs(close - open))
        body = abs(current.close - current.open)
        if body < avg_range * self.min_body_ratio:
            return False
        
        # Check that flow is in same direction as absorption
        if direction == "long" and current.delta <= 0:
            return False
        if direction == "short" and current.delta >= 0:
            return False
        
        return True

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong one-sided flow with failed extension.
        
        Core signal:
        1. Strong delta (>= 35% of volume one-sided)
        2. Wick opposite to flow (>= 8% of range)
        3. Stop distance within acceptable range
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Requirement 1: Strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Requirement 2: Wick opposite to flow
        has_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick / range_size >= self.min_wick_ratio:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick / range_size >= self.min_wick_ratio:
                has_wick = True

        if not has_wick:
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

    def _place_entry(self, ctx, setup: dict, tag_suffix: str = "") -> None:
        """Place entry order on absorption detection."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]
        
        tag = f"absorption-{tag_suffix}" if tag_suffix else "absorption"

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
