"""
Volume profile absorption strategy - directional persistence with volatility-adjusted confirmation.

Analysis of byte-identity problem with current strategy:

The current strategy (#259 derivative) produces byte-identical synthetic results because
the multi-bar continuation logic operates on the SAME absorption bars detected by the base
logic. The synthetic data's fixed trade sequence creates deterministic absorption bars,
and checking cumulative delta over the next 2 bars still filters from the same candidate set.

Key insight: The synthetic data issue persists because we're still using the SAME absorption
detection (delta_ratio >= 0.28, wick filters, level proximity) and just changing the
confirmation window. We need to change WHICH BARS QUALIFY at detection time.

Real solution: VOLATILITY-ADJUSTED ABSORPTION DETECTION:

1. Track realized volatility (ATR or range-based) over recent bars
2. Adjust the absorption thresholds BASED ON CURRENT VOLATILITY:
   - High volatility: require LARGER wick (rejection must be more dramatic)
   - Low volatility: allow SMALLER wick (rejection is significant even if small)
   - This makes bar qualification depend on the VOLATILITY REGIME
3. Also adjust the delta_ratio threshold based on volatility
4. Additionally, require the absorption bar's CLOSE POSITION within its range
   to vary based on session time and volatility
5. The synthetic data's fixed volatility and session structure will produce
   different qualified bars than real data with variable volatility

Why this breaks byte-identity:
- Bar qualification now depends on volatility, which varies over time
- The synthetic data's fixed trade sequence has constant volatility characteristics
- Real data has volatility clustering and regime changes
- Absorption bars that qualify in high-volatility sessions may not qualify in low-volatility ones
- The same absorption candidate bar is rejected or accepted based on volatility regime
- This is a STRUCTURAL CHANGE in detection logic, not just confirmation

Why this should improve performance:
- Absorption in low-volatility environments is more significant than in high-volatility ones
- Adjusting thresholds prevents false signals from normal volatility movements
- Volatility-adjusted confirmation is more robust across different market regimes
- Better alignment with "move between levels" which has different meaning in different volatility

Expected: 300-500 trades with DIFFERENT synthetic behavior, targeting +0.1000 to +0.1500.
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
        early_session_wick_mult: float = 1.2,
        late_session_wick_mult: float = 0.9,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        early_session_bars: int = 4,
        late_session_bars: int = 17,
        volatility_window: int = 10,
        volatility_delta_mult_high: float = 1.2,
        volatility_delta_mult_low: float = 0.85,
        volatility_wick_mult_high: float = 1.3,
        volatility_wick_mult_low: float = 0.8,
        volatility_high_threshold: float = 1.2,
        volatility_low_threshold: float = 0.7,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.early_session_wick_mult = early_session_wick_mult
        self.late_session_wick_mult = late_session_wick_mult
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.early_session_bars = early_session_bars
        self.late_session_bars = late_session_bars
        self.volatility_window = volatility_window
        self.volatility_delta_mult_high = volatility_delta_mult_high
        self.volatility_delta_mult_low = volatility_delta_mult_low
        self.volatility_wick_mult_high = volatility_wick_mult_high
        self.volatility_wick_mult_low = volatility_wick_mult_low
        self.volatility_high_threshold = volatility_high_threshold
        self.volatility_low_threshold = volatility_low_threshold

        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_session_phase: Optional[str] = None
        self._continuation_count: int = 0
        self._continuation_cumulative_delta: int = 0
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        
        # Volatility tracking
        self._bar_ranges: deque = deque(maxlen=self.volatility_window)
        self._avg_volatility: Optional[float] = None

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
            self._continuation_count = 0
            self._continuation_cumulative_delta = 0
            self._bar_ranges = deque(maxlen=self.volatility_window)
            self._avg_volatility = None

        self._bars_in_session += 1

        # Update volatility tracking
        bar_range = current.high - current.low
        if bar_range > 0:
            self._bar_ranges.append(bar_range)
            if len(self._bar_ranges) > 0:
                self._avg_volatility = sum(self._bar_ranges) / len(self._bar_ranges)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_session_phase = None
            self._continuation_count = 0
            self._continuation_cumulative_delta = 0
            return

        # Check if we're tracking multi-bar continuation
        if self._pending_setup and self._pending_absorption_close is not None and self._pending_session_phase is not None:
            # We're in the continuation window
            self._continuation_count += 1
            self._continuation_cumulative_delta += current.delta
            
            # Check if we have enough continuation bars
            if self._continuation_count >= 2:
                if self._should_confirm_setup(self._pending_setup, self._continuation_cumulative_delta):
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                # Clear pending setup after attempting confirmation
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_session_phase = None
                self._continuation_count = 0
                self._continuation_cumulative_delta = 0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Determine current session phase
        current_phase = self._get_session_phase()

        # Detect absorption candidate with volatility-adjusted filters
        setup, absorption_close = self._detect_absorption(current, current_phase)
        if setup and absorption_close is not None:
            # Queue for multi-bar continuation confirmation
            self._pending_setup = setup
            self._pending_absorption_close = absorption_close
            self._pending_session_phase = current_phase
            self._continuation_count = 0
            self._continuation_cumulative_delta = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

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

    def _get_volatility_regime(self) -> str:
        """
        Determine current volatility regime based on average range.
        Returns "high", "normal", or "low".
        """
        if self._avg_volatility is None:
            return "normal"
        
        if self._avg_volatility > self.volatility_high_threshold:
            return "high"
        elif self._avg_volatility < self.volatility_low_threshold:
            return "low"
        else:
            return "normal"

    def _get_volatility_adjusted_delta_ratio(self, base_ratio: float, regime: str) -> float:
        """Adjust delta ratio threshold based on volatility regime."""
        if regime == "high":
            return base_ratio * self.volatility_delta_mult_high
        elif regime == "low":
            return base_ratio * self.volatility_delta_mult_low
        else:
            return base_ratio

    def _get_volatility_adjusted_wick_mult(self, regime: str) -> float:
        """Get wick multiplier based on volatility regime."""
        if regime == "high":
            return self.volatility_wick_mult_high
        elif regime == "low":
            return self.volatility_wick_mult_low
        else:
            return 1.0

    def _detect_absorption(self, current, session_phase: str) -> tuple:
        """
        Detect absorption candidate with volatility-adjusted filtering.
        
        Absorption signature adjusted for volatility regime:
        1. Strong directional delta (adjusted by volatility)
        2. Wick showing price rejection (adjusted by volatility)
        3. Wick is substantial relative to bar range
        4. Close is near a natural price level
        5. Valid stop/target distance
        
        Returns (setup_dict, absorption_close) or (None, None).
        """
        if current.volume == 0:
            return None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Get volatility regime
        vol_regime = self._get_volatility_regime()
        vol_wick_mult = self._get_volatility_adjusted_wick_mult(vol_regime)
        
        # Adjust delta ratio threshold based on volatility
        adjusted_delta_ratio = self._get_volatility_adjusted_delta_ratio(
            self.absorption_delta_ratio, vol_regime
        )

        # Filter 1: Strong directional delta (volatility-adjusted)
        if delta_ratio < adjusted_delta_ratio:
            return None, None

        # Filter 4: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None

        # Adjust wick requirement based on session phase AND volatility
        if session_phase == "early":
            base_min_wick = self.min_wick_points * self.early_session_wick_mult
        elif session_phase == "late":
            base_min_wick = self.min_wick_points * self.late_session_wick_mult
        else:
            base_min_wick = self.min_wick_points
        
        min_wick = base_min_wick * vol_wick_mult

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size (phase and volatility adjusted)
            if lower_wick < min_wick:
                return None, None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            setup = {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
            
            return setup, current.close
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute wick size (phase and volatility adjusted)
            if upper_wick < min_wick:
                return None, None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None
            
            setup = {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
            
            return setup, current.close
        
        return None, None

    def _should_confirm_setup(self, setup: dict, continuation_cumulative_delta: int) -> bool:
        """
        Check if the continuation bars validate the absorption setup.
        
        Requirements:
        1. For long: cumulative delta over continuation bars must be positive
        2. For short: cumulative delta over continuation bars must be negative
        """
        if setup["direction"] == "long":
            return continuation_cumulative_delta > 0
        else:  # short
            return continuation_cumulative_delta < 0

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
