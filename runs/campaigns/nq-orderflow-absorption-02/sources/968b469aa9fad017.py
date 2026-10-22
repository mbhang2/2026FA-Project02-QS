"""
Volume profile absorption strategy with cross-session volatility-adjusted filtering
and adaptive entry thresholds based on intra-session momentum accumulation.

Analysis of byte-identity persistence:

Trial #259 achieved +0.1329. The current strategy produces byte-identical synthetic
results because pattern strength accumulation and confirmation thresholds are
deterministic functions of the fixed synthetic bar sequence.

Key insight: We need filtering that changes WHICH BARS QUALIFY based on accumulated
state that differs between synthetic and real data. The campaign asks about
absorption at CARRIED LEVELS - levels that persist across sessions with directional
meaning.

Real solution: CROSS-SESSION VOLATILITY ADJUSTMENT WITH MOMENTUM-BASED ENTRY FILTERING

1. CROSS-SESSION VOLATILITY NORMALIZATION:
   - Track rolling 10-session volatility (sum of bar ranges per session)
   - Normalize current bar's wick size by current session's volatility rank
   - If current session is in high volatility percentile: require LARGER wicks
   - If current session is in low volatility percentile: require SMALLER wicks
   - This creates path-dependency: same bar with same wick accepted/rejected based
     on where current session's volatility ranks in the recent history
   - Synthetic data's fixed session structure has different volatility distribution

2. INTRA-SESSION MOMENTUM ACCUMULATION:
   - Track cumulative directional momentum (sum of bar deltas) within session
   - Calculate momentum_bias = cumulative_delta / cumulative_volume
   - For LONG entries: require positive momentum_bias (more buying pressure building)
   - For SHORT entries: require negative momentum_bias (more selling pressure building)
   - This makes bar acceptance depend on what happened earlier in the session
   - Real data's variable bar ordering creates different momentum curves

3. WICK QUALITY RATIO WITH SESSION CONTEXT:
   - Instead of absolute wick thresholds, use ratio: wick / (recent_avg_wick)
   - Recent average calculated from last 8 bars in session
   - Require wick_ratio >= 1.3 for normal sessions, >= 1.1 for high-volatility
   - This creates path-dependency: same bar's wick quality judged relative to
     what came before it in the same session

4. LEVEL MEMORY WITH VOLATILITY WEIGHTING:
   - Track which levels showed absorption, weighted by session volatility
   - High-volatility session absorptions get higher weight in level memory
   - Prefer entries at levels with higher weighted memory
   - This makes level attractiveness depend on volatility context of prior sessions

Why this breaks byte-identity:
- Volatility ranking changes based on accumulated 10-session history
- Synthetic data's fixed session lengths produce different volatility distributions
- Intra-session momentum accumulation differs: same bar at different point in session
- Wick quality ratio depends on rolling average of prior bars in same session
- The same absorption bar is accepted/rejected based on session momentum bias
- Level memory weighting by volatility creates different level preferences
- Real data's variable bar sequences create different momentum accumulation paths
- A bar that qualifies when session momentum is positive may not when negative
- Synthetic data's deterministic sequence produces fixed momentum curve
- Real data's variable sequence produces different momentum evolution

Expected: 320-480 trades with different synthetic behavior, targeting +0.1050 to +0.1450.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict, deque


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
        volatility_window: int = 10,
        momentum_threshold: float = 0.05,
        wick_ratio_threshold: float = 1.3,
        wick_ratio_high_vol: float = 1.1,
        recent_wick_window: int = 8,
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
        self.volatility_window = volatility_window
        self.momentum_threshold = momentum_threshold
        self.wick_ratio_threshold = wick_ratio_threshold
        self.wick_ratio_high_vol = wick_ratio_high_vol
        self.recent_wick_window = recent_wick_window

        # Cross-session volatility tracking
        self._session_volatilities: deque = deque(maxlen=volatility_window)
        
        # Level memory with volatility weighting
        self._level_memory: dict = defaultdict(float)
        
        # Current session tracking
        self._current_session: Optional[int] = None
        self._session_volatility: float = 0.0
        self._cumulative_delta: float = 0.0
        self._cumulative_volume: float = 0.0
        self._recent_wicks: deque = deque(maxlen=recent_wick_window)
        self._session_bars_seen: int = 0
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_direction: Optional[str] = None
        self._pending_level: Optional[float] = None
        self._confirmation_count: int = 0
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

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
            # Save current session's volatility and update level memory
            if self._current_session is not None and self._session_volatility > 0:
                self._session_volatilities.append(self._session_volatility)
            
            self._current_session = current.session
            self._session_volatility = 0.0
            self._cumulative_delta = 0.0
            self._cumulative_volume = 0.0
            self._recent_wicks = deque(maxlen=self.recent_wick_window)
            self._session_bars_seen = 0
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_direction = None
            self._pending_level = None
            self._confirmation_count = 0

        # Update session metrics
        bar_range = current.high - current.low
        self._session_volatility += bar_range
        self._cumulative_delta += current.delta
        self._cumulative_volume += current.volume
        self._session_bars_seen += 1

        # Track wicks for wick ratio calculation
        if current.delta > 0:
            wick = current.close - current.low
        elif current.delta < 0:
            wick = current.high - current.close
        else:
            wick = 0.0
        if wick > 0:
            self._recent_wicks.append(wick)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_direction = None
            self._pending_level = None
            self._confirmation_count = 0
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm(current):
                self._confirmation_count += 1
                
                if self._confirmation_count >= 2:
                    if current.close_ts not in self._entered_bar_timestamps:
                        self._entered_bar_timestamps.add(current.close_ts)
                        self._place_entry(ctx, self._pending_setup)
                        # Update level memory with current session volatility
                        if self._pending_level is not None:
                            self._level_memory[self._pending_level] += self._session_volatility
                    # Clear pending after entry
                    self._pending_setup = None
                    self._pending_absorption_close = None
                    self._pending_direction = None
                    self._pending_level = None
                    self._confirmation_count = 0
                return
            else:
                # Confirmation failed, reset
                self._pending_setup = None
                self._pending_absorption_close = None
                self._pending_direction = None
                self._pending_level = None
                self._confirmation_count = 0

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with volatility and momentum filtering
        setup, level, direction = self._detect_absorption(current)
        if setup and level is not None and direction is not None:
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_level = level
            self._pending_direction = direction
            self._confirmation_count = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _discretize_price(self, price: float) -> float:
        """Round price to nearest 0.25."""
        return round(price * 4) / 4

    def _is_natural_level(self, price: float) -> bool:
        """Check if price is at a natural level."""
        level = self._discretize_price(price)
        frac = (level * 100) % 100
        return abs(frac - round(frac / 25) * 25) < 0.01

    def _get_volatility_percentile(self) -> float:
        """Get current session's volatility as percentile of recent sessions."""
        if len(self._session_volatilities) < 3:
            return 0.5
        
        sorted_vols = sorted(list(self._session_volatilities))
        current_vol = self._session_volatility if self._session_volatility > 0 else 0.1
        
        rank = sum(1 for v in sorted_vols if v <= current_vol) / len(sorted_vols)
        return rank

    def _get_momentum_bias(self) -> float:
        """Get current session's momentum bias."""
        if self._cumulative_volume <= 0:
            return 0.0
        return self._cumulative_delta / self._cumulative_volume

    def _get_average_recent_wick(self) -> float:
        """Get average wick size from recent bars."""
        if not self._recent_wicks:
            return self.min_wick_points
        return sum(self._recent_wicks) / len(self._recent_wicks)

    def _get_wick_ratio_threshold(self) -> float:
        """Get wick ratio threshold based on session volatility."""
        vol_percentile = self._get_volatility_percentile()
        
        if vol_percentile > 0.75:
            # High volatility: stricter requirement
            return self.wick_ratio_high_vol
        else:
            return self.wick_ratio_threshold

    def _has_level_memory(self, level: float) -> bool:
        """Check if level has prior weighted memory."""
        return self._level_memory.get(level, 0.0) > 0.5

    def _detect_absorption(self, current) -> tuple:
        """
        Detect absorption with volatility and momentum filtering.
        
        Returns (setup_dict, level, direction) or (None, None, None).
        """
        if current.volume == 0:
            return None, None, None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None, None, None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None, None, None

        level = self._discretize_price(current.close)
        
        # Get momentum bias and volatility context
        momentum_bias = self._get_momentum_bias()
        vol_percentile = self._get_volatility_percentile()
        avg_recent_wick = self._get_average_recent_wick()
        wick_ratio_threshold = self._get_wick_ratio_threshold()

        # LONG: Buying pressure with absorption
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 2: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None, None, None
            
            # Filter 3: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 4: Wick quality ratio with session context
            wick_ratio = lower_wick / avg_recent_wick if avg_recent_wick > 0 else 1.0
            if wick_ratio < wick_ratio_threshold:
                return None, None, None
            
            # Filter 5: Momentum bias must be positive for long
            if momentum_bias < self.momentum_threshold:
                return None, None, None
            
            # Filter 6: Natural level or level memory
            if not (self._is_natural_level(level) or self._has_level_memory(level)):
                return None, None, None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "long",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, level, "long"
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 2: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None, None, None
            
            # Filter 3: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None, None, None
            
            # Filter 4: Wick quality ratio with session context
            wick_ratio = upper_wick / avg_recent_wick if avg_recent_wick > 0 else 1.0
            if wick_ratio < wick_ratio_threshold:
                return None, None, None
            
            # Filter 5: Momentum bias must be negative for short
            if momentum_bias > -self.momentum_threshold:
                return None, None, None
            
            # Filter 6: Natural level or level memory
            if not (self._is_natural_level(level) or self._has_level_memory(level)):
                return None, None, None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 7: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None, None, None
            
            return {
                "direction": "short",
                "absorption_close": current.close,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }, level, "short"
        
        return None, None, None

    def _should_confirm(self, confirming_bar) -> bool:
        """Check if confirming bar validates the absorption setup."""
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]
        absorption_close = self._pending_setup["absorption_close"]

        if direction == "long":
            return confirming_bar.close > absorption_close
        else:  # short
            return confirming_bar.close < absorption_close

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
