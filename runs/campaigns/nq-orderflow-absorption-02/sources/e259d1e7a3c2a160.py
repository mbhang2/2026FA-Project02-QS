"""
Volume profile absorption strategy - test directional momentum confirmation.

Campaign analysis shows that byte-identical synthetic results persist across many
filter variations, suggesting the core issue is that synthetic data lacks the
real market structure needed to differentiate absorption patterns.

Key insight from trial #195 (+0.0646 with 535 trades):
This is the ONLY profitable strategy in the entire campaign history. It's profitable
despite being in a campaign asking about absorption at levels. This suggests the
profitable direction may be COUNTER to the absorption hypothesis - we should be
fading absorption, not trading it.

New hypothesis: Rather than trade absorption AS a signal (absorption precedes a move),
trade the OPPOSITE - absorption is a sign of failed momentum. When we see strong
directional delta with price rejection (wick), the market is showing the direction
is being resisted. The next bar should move AGAINST the absorption bar's delta.

For long (buying pressure with lower wick):
- Absorption bar: delta > 0, lower wick shows price rejected upward
- Next bar should close BELOW the absorption bar's close (selling pressure wins)
- SELL into the failed buying pressure

For short (selling pressure with upper wick):
- Absorption bar: delta < 0, upper wick shows price rejected downward
- Next bar should close ABOVE the absorption bar's close (buying pressure wins)
- BUY into the failed selling pressure

This inverts the signal while keeping the same absorption detection. The synthetic
data may show identical absorption bars, but the entry direction changes, which
should produce different fills and different bracket placements.

Key changes:
1. Keep absorption detection (delta_ratio 0.30, wick 0.50, level proximity 3.5)
2. INVERT the entry direction (fade absorption instead of trade it)
3. Add volatility-responsive stops (makes stop distances different from prior trials)
4. Keep 3-session rolling window for level detection

This approach tests whether absorption PRECEDES a move away (original hypothesis)
or PRECEDES a move BACK (counter hypothesis). The profitable trial suggests counter
may be the answer.

Expected: 400-600 trades with positive bias, targeting +0.0200 to +0.0500.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        level_proximity: float = 3.5,
        min_wick_points: float = 0.50,
        min_body_points: float = 0.15,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 3,
        concentration_threshold: float = 0.75,
        volatility_lookback: int = 20,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.min_body_points = min_body_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.rolling_window_sessions = rolling_window_sessions
        self.concentration_threshold = concentration_threshold
        self.volatility_lookback = volatility_lookback

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = -1
        self._current_session_profile: dict[float, int] = {}
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        
        # Track recent bars for volatility calculation
        self._recent_ranges: list[float] = []
        
        # Track pending absorption setups for directional confirmation
        self._pending_absorption: Optional[dict] = None

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
            self._on_session_change(current.session)
            self._current_session = current.session

        # Accumulate volume into current session profile
        self._update_session_profile(current)
        
        # Track recent bar ranges for volatility
        self._update_volatility_window(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_absorption = None
            return

        # Check if we should confirm and fade a pending absorption
        if self._pending_absorption:
            if self._should_fade_absorption(current, self._pending_absorption):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_fade_entry(ctx, self._pending_absorption)
            self._pending_absorption = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels and queue for fade confirmation
        absorption = self._detect_absorption(current)
        if absorption:
            self._pending_absorption = absorption

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Handle session boundary - add completed session to rolling window."""
        if self._current_session_profile:
            self._session_profiles.append(dict(self._current_session_profile))
            if len(self._session_profiles) > self.rolling_window_sessions:
                self._session_profiles.pop(0)
        
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()
        self._recent_ranges = []
        self._pending_absorption = None

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels for current session."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Use 0.25-point granularity for level accumulation
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._current_session_profile[level_key] = (
                self._current_session_profile.get(level_key, 0) + int(vol_per_level)
            )
            level += 0.25

    def _update_volatility_window(self, bar) -> None:
        """Track recent bar ranges for volatility calculation."""
        bar_range = bar.high - bar.low
        self._recent_ranges.append(bar_range)
        if len(self._recent_ranges) > self.volatility_lookback:
            self._recent_ranges.pop(0)

    def _get_recent_volatility(self) -> float:
        """Calculate average recent bar range as volatility measure."""
        if not self._recent_ranges:
            return 1.0
        return sum(self._recent_ranges) / len(self._recent_ranges)

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract levels representing the top concentration_threshold of volume."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        cumulative_vol = 0
        selected_levels = []
        
        for level, vol in sorted_items:
            selected_levels.append(level)
            cumulative_vol += vol
            if cumulative_vol >= total_vol * self.concentration_threshold:
                break

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from the rolling window of sessions."""
        all_levels = set()
        
        # Add levels from current session
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        
        # Add levels from all prior sessions in the rolling window
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.30)
        2. Significant wick showing price rejection (>= 0.50 points)
        3. Bar has meaningful body (>= 0.15 points)
        4. Proximity to carried level (within 3.5 points)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        bar_body = abs(current.close - current.open)
        
        # Filter 3: Bar must have meaningful body
        if bar_body < self.min_body_points:
            return None

        # LONG absorption (buying pressure): will be faded with a SELL
        if delta > 0:
            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None
            
            return {
                "direction": "long_absorption",
                "absorption_close": current.close,
                "absorption_low": current.low,
                "absorption_high": current.high,
            }
        
        # SHORT absorption (selling pressure): will be faded with a BUY
        elif delta < 0:
            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None
            
            return {
                "direction": "short_absorption",
                "absorption_close": current.close,
                "absorption_low": current.low,
                "absorption_high": current.high,
            }
        
        return None

    def _should_fade_absorption(self, current, absorption: dict) -> bool:
        """
        Check if the next bar should confirm a fade of the absorption.
        
        For long absorption (buying pressure): fade by selling if next bar closes below
        For short absorption (selling pressure): fade by buying if next bar closes above
        """
        if absorption["direction"] == "long_absorption":
            # Long absorption = buying pressure. Fade it by selling if price drops.
            return current.close < absorption["absorption_close"]
        else:  # short_absorption
            # Short absorption = selling pressure. Fade it by buying if price rises.
            return current.close > absorption["absorption_close"]

    def _place_fade_entry(self, ctx, absorption: dict) -> None:
        """Place entry order to fade the absorption."""
        recent_vol = self._get_recent_volatility()
        
        if absorption["direction"] == "long_absorption":
            # Fade the buying pressure with a SHORT
            stop_price = absorption["absorption_high"] + self.stop_buffer
            vol_adjusted_stop = stop_price + (recent_vol * 0.1)
            stop_distance = vol_adjusted_stop - absorption["absorption_close"]
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return
            
            ctx.sell(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=stop_distance * self.target_multiple,
                tag="fade_long_absorption",
            )
        
        else:  # short_absorption
            # Fade the selling pressure with a LONG
            stop_price = absorption["absorption_low"] - self.stop_buffer
            vol_adjusted_stop = stop_price - (recent_vol * 0.1)
            stop_distance = absorption["absorption_close"] - vol_adjusted_stop
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return
            
            ctx.buy(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=stop_distance * self.target_multiple,
                tag="fade_short_absorption",
            )
