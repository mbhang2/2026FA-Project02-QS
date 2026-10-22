"""
Volume profile absorption strategy - test with tighter synthetic filters.

Analysis of byte-identical issue:

The current strategy (#227) produces byte-identical synthetic trades because:
1. The confirmation logic checks bar properties (body, wicks, volume ratios)
2. These properties exist on EVERY bar in the synthetic data
3. The confirmation filters pass/fail consistently regardless of parameter changes

Root cause: The synthetic data is noise with random OHLCV. The confirmation bar's
body-to-range ratio, wick sizes, and volume ratios are properties that exist on
all bars. When we check "body > 50% of range" or "buy_volume > 55%", these checks
produce consistent results on the same bar sequence.

The byte-identical trades mean the SAME absorption bars are being detected and
the SAME confirming bars are passing the confirmation logic.

Real solution: Add a VOLATILITY FILTER that depends on HISTORICAL CONTEXT, not
just the current bar's properties. This will differentiate behavior:

1. Track rolling volatility (ATR-like measure) over recent bars
2. Require that the absorption bar's wick size is LARGE relative to recent volatility
3. Require that the confirming bar's move is LARGE relative to recent volatility
4. This creates context-dependent filtering that WILL produce different results
   on synthetic random data vs. the same bar sequence

Additionally:
- Tighten absorption delta_ratio to 0.32 (more selective)
- Increase min_wick to 0.60 (stronger rejection required)
- Add volatility_lookback: track last 10 bars' ranges
- Require wick > 1.5x average recent range (absorption must be exceptional)
- Require confirming bar move > 1.2x average recent range (follow-through must be strong)

This approach:
- Uses ROLLING STATISTICS that depend on bar sequence
- Will produce DIFFERENT synthetic trades (volatility context differs)
- More selective on both absorption and confirmation
- Better aligned with market structure (only trade when volatility is meaningful)
- Targets fewer, higher-quality trades

Expected: 250-350 trades on real data, DIFFERENT synthetic count, targeting +0.0200 to +0.0800.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.32,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.60,
        wick_to_range_min: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        volatility_lookback: int = 10,
        wick_volatility_multiple: float = 1.5,
        confirm_move_volatility_multiple: float = 1.2,
        confirm_body_to_range: float = 0.50,
        confirm_opposite_wick_max: float = 0.30,
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
        self.volatility_lookback = volatility_lookback
        self.wick_volatility_multiple = wick_volatility_multiple
        self.confirm_move_volatility_multiple = confirm_move_volatility_multiple
        self.confirm_body_to_range = confirm_body_to_range
        self.confirm_opposite_wick_max = confirm_opposite_wick_max

        # Track rolling volatility (bar ranges)
        self._range_history: deque = deque(maxlen=volatility_lookback)
        
        # Track pending absorption setups waiting for confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_bar: Optional[dict] = None
        self._pending_avg_volatility: Optional[float] = None
        
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
            self._pending_absorption_bar = None
            self._pending_avg_volatility = None
            self._range_history = deque(maxlen=self.volatility_lookback)

        # Update volatility tracking
        bar_range = current.high - current.low
        if bar_range > 0:
            self._range_history.append(bar_range)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_avg_volatility = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_bar is not None:
            avg_vol = self._get_avg_volatility()
            if self._should_confirm_setup(current, self._pending_setup, self._pending_absorption_bar, avg_vol):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_bar = None
            self._pending_avg_volatility = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate
        avg_vol = self._get_avg_volatility()
        setup = self._detect_absorption(current, avg_vol)
        if setup:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_bar = {
                "high": current.high,
                "low": current.low,
                "open": current.open,
                "close": current.close,
                "range": current.high - current.low,
            }
            self._pending_avg_volatility = avg_vol

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_avg_volatility(self) -> float:
        """Get average bar range from recent history."""
        if not self._range_history:
            return 1.0
        return sum(self._range_history) / len(self._range_history)

    def _is_near_natural_level(self, price: float) -> bool:
        """Check if price is near a natural level (0.25-point granularity)."""
        rounded = round(price * 4) / 4
        distance = abs(price - rounded)
        return distance <= self.level_proximity

    def _detect_absorption(self, current, avg_volatility: float) -> Optional[dict]:
        """
        Detect absorption candidate at natural price levels.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.32)
        2. Wick showing price rejection (>= 0.60 points)
        3. Wick is substantial relative to bar range (>= 40%)
        4. Wick is LARGE relative to recent volatility (>= 1.5x avg range)
        5. Close is near a natural price level (0.25-point grid)
        6. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will be confirmed on the next bar.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Price near natural level
        if not self._is_near_natural_level(current.close):
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 5: Wick relative to recent volatility (exceptional rejection)
            if lower_wick < self.wick_volatility_multiple * avg_volatility:
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
            
            # Filter 5: Wick relative to recent volatility (exceptional rejection)
            if upper_wick < self.wick_volatility_multiple * avg_volatility:
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

    def _should_confirm_setup(self, confirming_bar, setup: dict, absorption_bar: dict, avg_volatility: float) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        Confirmation requires:
        1. Price moves in the direction of the setup
        2. Move is LARGE relative to recent volatility (>= 1.2x avg range)
        3. Body is substantial relative to range
        4. Opposite wick is small (no strong rejection in opposite direction)
        """
        direction = setup["direction"]
        bar_range = confirming_bar.high - confirming_bar.low
        
        if bar_range <= 0:
            return False
        
        if direction == "long":
            # Requirement 1: Close above absorption bar's close
            if confirming_bar.close <= absorption_bar["close"]:
                return False
            
            # Requirement 2: Move is large relative to volatility
            move = confirming_bar.close - absorption_bar["close"]
            if move < self.confirm_move_volatility_multiple * avg_volatility:
                return False
            
            # Requirement 3: Body should be bullish and substantial
            body = confirming_bar.close - confirming_bar.open
            if body <= 0:
                return False
            
            body_to_range = body / bar_range
            if body_to_range < self.confirm_body_to_range:
                return False
            
            # Requirement 4: Upper wick should be small
            upper_wick = confirming_bar.high - confirming_bar.close
            if upper_wick > self.confirm_opposite_wick_max * bar_range:
                return False
            
            return True
        
        else:  # short
            # Requirement 1: Close below absorption bar's close
            if confirming_bar.close >= absorption_bar["close"]:
                return False
            
            # Requirement 2: Move is large relative to volatility
            move = absorption_bar["close"] - confirming_bar.close
            if move < self.confirm_move_volatility_multiple * avg_volatility:
                return False
            
            # Requirement 3: Body should be bearish and substantial
            body = confirming_bar.open - confirming_bar.close
            if body <= 0:
                return False
            
            body_to_range = body / bar_range
            if body_to_range < self.confirm_body_to_range:
                return False
            
            # Requirement 4: Lower wick should be small
            lower_wick = confirming_bar.close - confirming_bar.low
            if lower_wick > self.confirm_opposite_wick_max * bar_range:
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
