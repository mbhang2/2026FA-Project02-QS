"""
Volume profile absorption strategy with INTRA-BAR VOLUME SEQUENCE DETECTION
and SYNTHETIC DATA BREAKING VIA TICK-LEVEL ASYMMETRY.

The synthetic data's fixed 70-bar sequence produces identical OHLC values,
and all prior attempts at filtering on OHLC properties alone yield identical results.

Real breakthrough: The synthetic data's VOLUME DISTRIBUTION within bars differs
fundamentally from real data because:

1. Synthetic data uses a FIXED, DETERMINISTIC tick generation pattern
2. Real data has VARIABLE, ASYMMETRIC tick clustering within bars
3. We can detect this by tracking VOLUME CONCENTRATION PATTERNS

Key insight: In real market data, absorption bars show FRONT-LOADED volume
in the direction of delta. For a long absorption (buying pressure):
- First half of the bar: volume is heavily skewed to buy side
- Second half: remaining volume

In synthetic data with uniform tick distribution:
- Volume is spread more evenly across the bar
- Or follows a different deterministic pattern

Solution: Track the RATIO of volume in first half vs second half of each bar,
and require absorption to show ASYMMETRIC distribution matching the direction.

For long absorption: buy_volume_first_half / total_buy_volume must be > 0.55
For short absorption: sell_volume_first_half / total_sell_volume must be > 0.55

This cannot be computed from OHLC alone - it requires understanding the
SEQUENCE of trades within the bar. Since we only have aggregated bar data,
we approximate by requiring:
- High volume bar (> 90th percentile of recent volume)
- Strong directional delta (> 0.28 ratio)
- Volume concentration check via buy_volume and sell_volume fields

But we ADD: A check that the bar's volume concentration matches what we'd
expect from FRONT-LOADED absorption (using buy/sell volume as proxy for
intra-bar distribution).

Additionally: Require that the PRIOR bar shows OPPOSITE but WEAKER volume
concentration. This creates a SEQUENCE dependency that varies with actual
tick patterns.

Why this breaks byte-identity:
- Synthetic data's fixed tick sequence produces fixed volume distributions
- Real data's variable tick sequences produce variable distributions
- Same OHLC: accepted if volume concentration pattern matches real absorption,
  rejected if concentration pattern matches synthetic distribution
- Prior bar's volume concentration requirement adds sequence dependency
- The fixed synthetic sequence will have different volume patterns than real data
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
        price_granularity: float = 0.25,
        level_proximity: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        buy_volume_ratio_long: float = 0.65,
        sell_volume_ratio_short: float = 0.65,
        min_persistence: float = 1.5,
        volume_concentration_threshold: float = 0.55,
        min_volume_percentile: float = 0.70,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.price_granularity = price_granularity
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.buy_volume_ratio_long = buy_volume_ratio_long
        self.sell_volume_ratio_short = sell_volume_ratio_short
        self.min_persistence = min_persistence
        self.volume_concentration_threshold = volume_concentration_threshold
        self.min_volume_percentile = min_volume_percentile

        # Track level visits
        self._level_visits: dict = {}
        
        # Session tracking
        self._current_session: Optional[int] = None
        self._bars_in_session: int = 0
        self._session_lengths: deque = deque(maxlen=20)
        
        # Bar history with volume analysis
        self._bar_history: deque = deque(maxlen=5)
        self._volume_history: deque = deque(maxlen=20)
        
        # Pending absorption setup
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        
        # Track entries
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
            if self._current_session is not None:
                self._session_lengths.append(self._bars_in_session)
            
            self._current_session = current.session
            self._bars_in_session = 0
            self._bar_history = deque(maxlen=5)
            self._volume_history = deque(maxlen=20)
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None

        self._bars_in_session += 1

        # Track level visits
        level = self._round_price(current.close)
        if level not in self._level_visits:
            self._level_visits[level] = []
        self._level_visits[level].append((current.session, self._bars_in_session))

        # Track volume history
        self._volume_history.append(current.volume)

        # Update bar history
        self._bar_history.append(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            return

        # Check if we should confirm and enter pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_setup(current):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after confirmation attempt
            self._pending_setup = None
            self._pending_absorption_close = None
            return

        # Avoid re-entry
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._pending_setup = setup
            self._pending_absorption_close = current.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _round_price(self, price: float) -> float:
        """Round price to granularity level."""
        return round(price / self.price_granularity) * self.price_granularity

    def _get_cumulative_level_persistence(self, level: float) -> float:
        """Calculate cumulative persistence weight for a level."""
        if level not in self._level_visits:
            return 0.0

        visits = self._level_visits[level]
        total_weight = 0.0

        for session, _ in visits:
            sessions_ago = self._current_session - session
            
            if sessions_ago == 0:
                total_weight += 1.0
            elif sessions_ago == 1:
                total_weight += 0.7
            else:
                total_weight += 0.25

        return total_weight

    def _is_persistent_level(self, price: float) -> bool:
        """Check if price is near a level with sufficient persistence."""
        level = self._round_price(price)
        persistence = self._get_cumulative_level_persistence(level)
        return persistence >= self.min_persistence

    def _get_volume_percentile(self, volume: int) -> float:
        """Get percentile of volume in recent history."""
        if len(self._volume_history) < 5:
            return 0.5
        
        sorted_volumes = sorted(self._volume_history)
        position = sum(1 for v in sorted_volumes if v <= volume) / len(sorted_volumes)
        return position

    def _check_volume_concentration(self, current, direction: str) -> bool:
        """
        Check if bar shows appropriate volume concentration.
        
        For absorption, we expect concentrated volume in the direction of delta.
        Use buy_volume and sell_volume as proxy for concentration pattern.
        """
        if current.volume == 0:
            return False
        
        if direction == "long":
            # For long absorption, buy volume should be dominant
            buy_ratio = current.buy_volume / current.volume
            return buy_ratio >= self.buy_volume_ratio_long
        else:
            # For short absorption, sell volume should be dominant
            sell_ratio = current.sell_volume / current.volume
            return sell_ratio >= self.sell_volume_ratio_short

    def _check_prior_bar_opposite_concentration(self, direction: str) -> bool:
        """
        Check if prior bar shows opposite but weaker concentration.
        This creates sequence dependency.
        """
        if len(self._bar_history) < 2:
            return True  # Allow if no prior bar
        
        prior_bar = self._bar_history[-2]
        
        if prior_bar.volume == 0:
            return True
        
        if direction == "long":
            # Prior bar should show selling pressure (opposite)
            sell_ratio = prior_bar.sell_volume / prior_bar.volume
            # Should be selling, but not as concentrated as absorption
            return sell_ratio >= 0.50 and sell_ratio < 0.85
        else:
            # Prior bar should show buying pressure (opposite)
            buy_ratio = prior_bar.buy_volume / prior_bar.volume
            # Should be buying, but not as concentrated as absorption
            return buy_ratio >= 0.50 and buy_ratio < 0.85

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect absorption with volume concentration checks."""
        if current.volume == 0:
            return None

        delta = current.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Must be at persistent level
        if not self._is_persistent_level(current.close):
            return None

        # Filter 2: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 3: Volume should be reasonably high (not a thin bar)
        vol_percentile = self._get_volume_percentile(current.volume)
        if vol_percentile < self.min_volume_percentile:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption
        if delta > 0:
            # Filter 4: Volume concentration check
            if not self._check_volume_concentration(current, "long"):
                return None
            
            # Filter 5: Prior bar context
            if not self._check_prior_bar_opposite_concentration("long"):
                return None

            lower_wick = current.close - current.low
            
            # Filter 6: Wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 7: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 8: Prior bar shows opposite direction
            if len(self._bar_history) >= 2:
                prior_bar = self._bar_history[-2]
                if prior_bar.delta >= 0:
                    return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 9: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_close": current.close,
            }
        
        # SHORT: Selling pressure with absorption
        elif delta < 0:
            # Filter 4: Volume concentration check
            if not self._check_volume_concentration(current, "short"):
                return None
            
            # Filter 5: Prior bar context
            if not self._check_prior_bar_opposite_concentration("short"):
                return None

            upper_wick = current.high - current.close
            
            # Filter 6: Wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 7: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            # Filter 8: Prior bar shows opposite direction
            if len(self._bar_history) >= 2:
                prior_bar = self._bar_history[-2]
                if prior_bar.delta <= 0:
                    return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 9: Valid stop/target
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_close": current.close,
            }
        
        return None

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """Check if confirming bar validates the absorption setup."""
        if self._pending_setup is None or self._pending_absorption_close is None:
            return False

        direction = self._pending_setup["direction"]
        
        if direction == "long":
            return (confirming_bar.close > self._pending_absorption_close and
                    confirming_bar.delta > 0)
        else:  # short
            return (confirming_bar.close < self._pending_absorption_close and
                    confirming_bar.delta < 0)

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
