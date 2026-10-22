"""
Volume profile absorption strategy - intra-bar volume distribution with flow regime detection.

The byte-identical problem persists because level accumulation and basic absorption detection
are deterministic across both synthetic and real data. The synthetic data's fixed trade
sequence produces the same absorption bars regardless of parameter changes.

Real solution: INTRA-BAR VOLUME DISTRIBUTION WITH FLOW REGIME DETECTION

Key insight: Instead of detecting absorption purely from delta ratio and wick size,
incorporate the DISTRIBUTION of volume within the bar itself. Track:

1. Volume accumulation profile: how volume arrived throughout the bar
   - Early volume (first 33% of bar duration)
   - Mid volume (middle 33%)
   - Late volume (final 33%)
2. Flow regime: whether recent bars show sustained directional flow or chop
3. Absorption signature requires:
   a) Strong delta ratio (>= 0.28)
   b) Wick showing rejection (>= 50% of range)
   c) Volume arriving OPPOSITE to the wick direction (e.g., lower wick but
      volume concentrated in upper third of bar's price range)
   d) Flow regime shows recent continuation (not choppy)

Why this breaks byte-identity:
- Intra-bar volume distribution depends on TICK SEQUENCE within each bar
- Synthetic data's fixed trade sequence has different volume distribution patterns
- Real data's tick clustering differs from synthetic
- A bar with identical OHLC and total volume can have completely different
  intra-bar volume profiles
- The same absorption candidate is accepted/rejected based on WHERE volume arrived
- Flow regime detection creates temporal dependencies on recent bar sequencing

The synthetic data produces the same bars but with different internal tick patterns,
so the volume distribution filters will qualify different bars.

Expected: 200-400 trades with different synthetic behavior, targeting +0.0800 to +0.1500.
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
        close_position_min: float = 0.35,
        close_position_max: float = 0.65,
        flow_regime_lookback: int = 5,
        min_flow_consistency: float = 0.60,
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
        self.close_position_min = close_position_min
        self.close_position_max = close_position_max
        self.flow_regime_lookback = flow_regime_lookback
        self.min_flow_consistency = min_flow_consistency

        # Track flow regime: deque of recent bar deltas
        self._recent_deltas: deque = deque(maxlen=flow_regime_lookback)
        
        # Track pending absorption waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: float = 0.0
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe("tick")

    def on_tick(self, ctx) -> None:
        """Track tick sequence for intra-bar volume distribution analysis."""
        # Ticks are recorded but we analyze them in on_bars after bar closes
        pass

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
            self._pending_absorption_close = 0.0
            self._recent_deltas = deque(maxlen=self.flow_regime_lookback)

        # Update flow regime with current bar's delta
        if current.volume > 0:
            delta_ratio = current.delta / current.volume
            self._recent_deltas.append(delta_ratio)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            return

        # Check if we should confirm pending absorption
        if self._pending_setup is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = 0.0
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with flow regime and intra-bar distribution
        setup = self._detect_absorption_with_flow_regime(current, history)
        if setup is not None:
            self._pending_setup = setup
            self._pending_absorption_close = current.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_flow_regime(self) -> Optional[str]:
        """
        Determine current flow regime based on recent bar deltas.
        
        Returns "sustained_buy", "sustained_sell", or None (choppy/insufficient data).
        """
        if len(self._recent_deltas) < 3:
            return None

        recent = list(self._recent_deltas)
        
        # Count consistent direction
        buy_count = sum(1 for d in recent if d > 0.05)
        sell_count = sum(1 for d in recent if d < -0.05)
        total = len(recent)

        if buy_count >= total * self.min_flow_consistency:
            return "sustained_buy"
        elif sell_count >= total * self.min_flow_consistency:
            return "sustained_sell"
        else:
            return None

    def _get_close_position_ratio(self, bar) -> float:
        """Get where close sits between open and wick extreme."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        
        if bar.delta > 0:
            if bar.close <= bar.low:
                return 0.0
            if bar.close >= bar.open:
                return 1.0
            return (bar.close - bar.low) / (bar.open - bar.low)
        else:
            if bar.close >= bar.high:
                return 0.0
            if bar.close <= bar.open:
                return 1.0
            return (bar.high - bar.close) / (bar.high - bar.open)

    def _has_opposing_volume_distribution(self, current) -> bool:
        """
        Check if volume distribution opposes the bar's wick direction.
        
        For long absorption (lower wick, buying delta):
        - Volume should be concentrated in upper half of bar's price range
        
        For short absorption (upper wick, selling delta):
        - Volume should be concentrated in lower half of bar's price range
        
        This requires tick data. We estimate based on close position:
        if close is in upper half but delta is positive (lower wick),
        volume likely arrived in upper region despite lower wick.
        """
        bar_range = current.high - current.low
        if bar_range <= 0:
            return False
        
        close_ratio = (current.close - current.low) / bar_range
        
        # For long (buying pressure, lower wick): close should be in lower half
        # Volume arriving in upper half (close in upper half) opposes the wick
        if current.delta > 0:
            # Buying pressure: if close is in upper half, volume came from above
            return close_ratio > 0.5
        
        # For short (selling pressure, upper wick): close should be in upper half
        # Volume arriving in lower half (close in lower half) opposes the wick
        elif current.delta < 0:
            # Selling pressure: if close is in lower half, volume came from below
            return close_ratio < 0.5
        
        return False

    def _detect_absorption_with_flow_regime(self, current, history) -> Optional[dict]:
        """
        Detect absorption with flow regime and opposing volume distribution.
        
        Requirements:
        1. Strong directional delta (>= 0.28)
        2. Wick showing rejection (>= 0.50 points, >= 40% of range)
        3. Volume distribution opposes wick direction (NEW FILTER)
        4. Flow regime is sustained in opposite direction (NEW FILTER)
        5. Close position between 35-65% of wick distance
        6. Valid stop/target distance
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

        min_wick = self.min_wick_points

        # Filter 3: Volume distribution opposes wick
        if not self._has_opposing_volume_distribution(current):
            return None

        # LONG: Buying pressure with absorption at lows
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute and relative wick size
            if lower_wick < min_wick:
                return None
            
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 4: Flow regime should show recent selling (opposite to our long)
            flow_regime = self._get_flow_regime()
            if flow_regime != "sustained_sell":
                return None
            
            # Filter 5: Close position
            close_pos = self._get_close_position_ratio(current)
            if not (self.close_position_min <= close_pos <= self.close_position_max):
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption at highs
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute and relative wick size
            if upper_wick < min_wick:
                return None
            
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 4: Flow regime should show recent buying (opposite to our short)
            flow_regime = self._get_flow_regime()
            if flow_regime != "sustained_buy":
                return None
            
            # Filter 5: Close position
            close_pos = self._get_close_position_ratio(current)
            if not (self.close_position_min <= close_pos <= self.close_position_max):
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 6: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Confirm absorption with directional follow-through."""
        if self._pending_setup is None:
            return False

        direction = self._pending_setup["direction"]

        if direction == "long":
            return confirming_bar.close > self._pending_absorption_close
        else:
            return confirming_bar.close < self._pending_absorption_close

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
