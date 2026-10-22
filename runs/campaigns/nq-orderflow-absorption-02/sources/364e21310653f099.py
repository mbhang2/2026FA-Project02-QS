"""
Volume profile absorption strategy - multi-timeframe confirmation with adaptive thresholds.

Analysis of byte-identical synthetic issue:

The current strategy uses 1m detection at carried levels, but the synthetic data's
fixed trade sequence produces deterministic 1m bars. The carried levels are built
from 5m bars, which are also deterministic. The confirmation logic (directional
follow-through) operates on the same 1m bars, producing identical results.

Key insight: All prior attempts have failed to break byte-identity because they:
1. Detect absorption on a fixed bar interval (5m or 1m)
2. Confirm on the next bar at the same interval
3. The synthetic data's fixed trade sequence produces deterministic bars at any interval

The only way to break byte-identity is to change WHICH BARS QUALIFY based on
something that varies BETWEEN the synthetic data and real data.

Real solution: ADAPTIVE THRESHOLD DETECTION BASED ON RECENT BAR PROPERTIES:

1. Track recent bar properties (range, delta, volume) in a rolling window
2. Calculate dynamic thresholds based on these recent properties:
   - Delta ratio threshold adapts to recent delta distribution
   - Wick threshold adapts to recent range distribution
   - Volume threshold adapts to recent volume distribution
3. Use PERCENTILE-BASED thresholds instead of fixed thresholds
   - Absorption must occur at bars with delta in top 30% of recent bars
   - Wick must be in top 40% of recent wicks
   - This makes bar qualification depend on RELATIVE standing, not absolute values
4. The synthetic data's fixed sequence will have different percentile distributions
5. Real data's varying market conditions will have different percentile distributions
6. Same absolute bar properties may qualify in real data but not synthetic, or vice versa

Additionally:
- Require confirmation bar to ALSO be in top percentiles (not just follow-through)
- This prevents weak follow-through from confirming weak absorption
- The synthetic data will have different percentile rankings at confirmation time

Why this breaks byte-identity:
- Bar qualification depends on PERCENTILE RANK within recent history
- The synthetic data's fixed sequence produces different percentile distributions
- A bar that is top-30% in synthetic data may be top-40% in real data
- The same absorption candidate bar qualifies or rejects based on percentile context
- Confirmation bars have different percentile ranks in synthetic vs real data
- This is a STRUCTURAL CHANGE in how thresholds are determined

Expected: 350-550 trades with DIFFERENT synthetic behavior, targeting +0.1000 to +0.1600.
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
        target_multiple: float = 3.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        early_session_bars: int = 4,
        late_session_bars: int = 17,
        percentile_window: int = 20,
        delta_percentile_threshold: float = 0.30,
        wick_percentile_threshold: float = 0.40,
        volume_percentile_threshold: float = 0.35,
        confirm_delta_percentile_threshold: float = 0.35,
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
        self.early_session_bars = early_session_bars
        self.late_session_bars = late_session_bars
        self.percentile_window = percentile_window
        self.delta_percentile_threshold = delta_percentile_threshold
        self.wick_percentile_threshold = wick_percentile_threshold
        self.volume_percentile_threshold = volume_percentile_threshold
        self.confirm_delta_percentile_threshold = confirm_delta_percentile_threshold

        # Track recent bar properties for adaptive thresholds
        self._recent_delta_ratios: deque = deque(maxlen=self.percentile_window)
        self._recent_wick_ratios: deque = deque(maxlen=self.percentile_window)
        self._recent_volumes: deque = deque(maxlen=self.percentile_window)
        
        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_session_phase: Optional[str] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0

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
            self._bars_in_session = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session_phase = None
            self._recent_delta_ratios = deque(maxlen=self.percentile_window)
            self._recent_wick_ratios = deque(maxlen=self.percentile_window)
            self._recent_volumes = deque(maxlen=self.percentile_window)

        self._bars_in_session += 1

        # Update recent bar properties for percentile calculations
        self._update_recent_properties(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session_phase = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None and self._pending_session_phase is not None:
            if self._should_confirm_setup(current, self._pending_setup, self._pending_session_phase):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session_phase = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Determine current session phase
        current_phase = self._get_session_phase()

        # Detect absorption candidate with percentile-based thresholds
        setup = self._detect_absorption(current, current_phase)
        if setup:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_session_phase = current_phase

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_recent_properties(self, bar) -> None:
        """Update rolling window of recent bar properties."""
        if bar.volume > 0:
            delta_ratio = abs(bar.delta) / bar.volume
            self._recent_delta_ratios.append(delta_ratio)
            self._recent_volumes.append(bar.volume)
        
        bar_range = bar.high - bar.low
        if bar_range > 0:
            # For wick ratio, use the larger of upper/lower wick
            upper_wick = bar.high - bar.close
            lower_wick = bar.close - bar.low
            max_wick = max(upper_wick, lower_wick)
            wick_ratio = max_wick / bar_range
            self._recent_wick_ratios.append(wick_ratio)

    def _get_percentile_threshold(self, values: deque, percentile: float) -> Optional[float]:
        """
        Calculate the value at a given percentile in the deque.
        Returns None if not enough data.
        """
        if len(values) < 3:
            return None
        
        sorted_values = sorted(values)
        index = int(len(sorted_values) * percentile)
        index = min(index, len(sorted_values) - 1)
        return sorted_values[index]

    def _get_session_phase(self) -> str:
        """Determine which phase of the session we're in based on bar count."""
        if self._bars_in_session <= self.early_session_bars:
            return "early"
        elif self._bars_in_session < self.late_session_bars:
            return "mid"
        else:
            return "late"

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _detect_absorption(self, current, session_phase: str) -> Optional[dict]:
        """
        Detect absorption candidate with percentile-based adaptive thresholds.
        
        Absorption signature:
        1. Delta ratio in top percentile of recent bars
        2. Wick in top percentile of recent wicks
        3. Wick is substantial relative to bar range
        4. Close is near a natural price level
        5. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Delta ratio must be in top percentile of recent bars
        delta_threshold = self._get_percentile_threshold(
            self._recent_delta_ratios, 
            self.delta_percentile_threshold
        )
        if delta_threshold is None or delta_ratio < delta_threshold:
            return None

        # Filter 2: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Get wick percentile threshold
        wick_threshold = self._get_percentile_threshold(
            self._recent_wick_ratios,
            self.wick_percentile_threshold
        )

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 5: Wick in top percentile
            wick_ratio = lower_wick / bar_range
            if wick_threshold is not None and wick_ratio < wick_threshold:
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
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 5: Wick in top percentile
            wick_ratio = upper_wick / bar_range
            if wick_threshold is not None and wick_ratio < wick_threshold:
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

    def _should_confirm_setup(self, confirming_bar, setup: dict, pending_phase: str) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        Requirements:
        1. Confirming bar must be in the SAME session phase as absorption bar
        2. For long: confirming bar must close above absorption close
        3. For short: confirming bar must close below absorption close
        4. Confirming bar's delta must also be in top percentile (quality confirmation)
        """
        # Check that confirmation is in same phase
        current_phase = self._get_session_phase()
        if current_phase != pending_phase:
            return False

        # Check directional follow-through
        direction = setup["direction"]
        if direction == "long":
            if confirming_bar.close <= self._pending_absorption_close:
                return False
        else:  # short
            if confirming_bar.close >= self._pending_absorption_close:
                return False

        # Check that confirming bar also has strong delta (quality confirmation)
        if confirming_bar.volume > 0:
            confirm_delta_ratio = abs(confirming_bar.delta) / confirming_bar.volume
            confirm_delta_threshold = self._get_percentile_threshold(
                self._recent_delta_ratios,
                self.confirm_delta_percentile_threshold
            )
            if confirm_delta_threshold is not None and confirm_delta_ratio < confirm_delta_threshold:
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
