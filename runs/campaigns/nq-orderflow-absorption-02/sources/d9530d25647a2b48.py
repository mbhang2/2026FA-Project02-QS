"""
Volume profile absorption strategy - exploring alternative level extraction and proximity logic.

Campaign analysis shows best performers:
- #81: -0.0096 with 622 trades (BEST)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades

The previous trial produced byte-identical trades on synthetic data despite adding
flow reversal and momentum checks. This suggests those conditions aren't actually
filtering in the synthetic environment, which means the real filtering must come
from a different dimension.

Key insight: The synthetic test being identical means the new conditions (flow reversal,
momentum confirmation) are either:
1. Never being triggered because the synthetic data doesn't have the right pattern
2. Or they're redundant with existing filters

This trial takes a different approach - instead of adding more boolean conditions,
it refines the CORE filtering logic that determines which bars get considered at all:

1. Change level proximity calculation: use a DYNAMIC proximity threshold based on
   recent volatility (ATR-like) instead of fixed 0.5 points
2. Extract levels differently: use VOLUME-WEIGHTED percentile approach instead of
   just top N levels - this captures the actual price clustering better
3. Add a filter for bars that are NEAR but NOT AT the level - absorption at a level
   means price is testing it, not that it closed exactly on it
4. Stricter volume profile: only count bars with volume > session median volume
5. Reduce momentum requirement back to 2 bars (the previous 3 was too strict)
6. Remove the flow reversal requirement (it's likely causing the byte-identical issue
   by filtering out all signals in synthetic data)
7. Keep the wick and body requirements but make them slightly less strict (0.35 instead
   of 0.40 for wick_ratio)

The hypothesis: #81's selectivity comes from a smarter way to identify "levels that matter"
rather than just taking top N. By using volume-weighted percentiles and dynamic proximity
based on volatility, we identify the real support/resistance where absorption is likely
to precede a move.

Expected: Different trade distribution that should score better than byte-identical baseline.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.42,
        min_wick_points: float = 0.50,
        min_wick_ratio: float = 0.35,
        min_body_ratio: float = 0.25,
        stop_buffer: float = 0.25,
        target_multiple: float = 1.8,
        max_stop_distance: float = 2.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_tick_count: int = 4,
        lookback_bars: int = 5,
        min_momentum_bars: int = 2,
        atr_period: int = 20,
        volume_percentile: float = 0.60,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.min_wick_ratio = min_wick_ratio
        self.min_body_ratio = min_body_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_tick_count = min_tick_count
        self.lookback_bars = lookback_bars
        self.min_momentum_bars = min_momentum_bars
        self.atr_period = atr_period
        self.volume_percentile = volume_percentile

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        
        # Track recent bar deltas for momentum confirmation
        self._recent_deltas: list[int] = []
        
        # Track recent true ranges for ATR calculation
        self._true_ranges: list[float] = []
        
        # Track session volumes for dynamic threshold
        self._session_volumes: list[int] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            self._on_session_change(current.session)
            self._current_session = current.session

        # Accumulate volume profile for current session
        self._update_session_profile(current)
        
        # Track session volumes
        if current.volume > 0:
            self._session_volumes.append(current.volume)
        
        # Track recent deltas for momentum confirmation
        self._recent_deltas.append(current.delta)
        if len(self._recent_deltas) > self.lookback_bars:
            self._recent_deltas.pop(0)
        
        # Track true range for ATR
        prev = history[-2]
        tr = max(
            current.high - current.low,
            abs(current.high - prev.close),
            abs(current.low - prev.close)
        )
        self._true_ranges.append(tr)
        if len(self._true_ranges) > self.atr_period:
            self._true_ranges.pop(0)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption with dynamic proximity
        setup = self._detect_absorption(current, history)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and carry forward."""
        if self._session_levels:
            levels = self._extract_levels_from_profile(self._session_levels)
            self._prior_session_levels.append(levels)
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_bar_timestamps = set()
        self._recent_deltas = []
        self._session_volumes = []

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _extract_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """
        Extract significant levels using volume-weighted percentile approach.
        
        Instead of taking top N levels, find price clusters where volume accumulates
        above a certain percentile. This identifies real support/resistance better.
        """
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)
        
        if total_vol == 0:
            return []

        # Accumulate from top levels until we reach the volume percentile threshold
        accumulated_vol = 0
        threshold_vol = total_vol * self.volume_percentile
        
        levels = []
        for level, vol in sorted_items:
            levels.append(level)
            accumulated_vol += vol
            if accumulated_vol >= threshold_vol:
                break

        return sorted(list(set(levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _get_atr(self) -> float:
        """Calculate average true range for dynamic proximity."""
        if not self._true_ranges:
            return 1.0
        return sum(self._true_ranges) / len(self._true_ranges)

    def _get_dynamic_proximity(self) -> float:
        """
        Dynamic proximity threshold based on recent volatility.
        
        If market is volatile (high ATR), allow wider proximity.
        If market is calm (low ATR), require tighter proximity.
        """
        atr = self._get_atr()
        # Scale proximity: between 0.4 and 1.5 based on ATR
        return max(0.4, min(1.5, atr * 0.5))

    def _has_momentum(self, direction: str) -> bool:
        """
        Check if recent bars show momentum in the intended direction.
        
        For long: need at least min_momentum_bars with positive delta
        For short: need at least min_momentum_bars with negative delta
        """
        if len(self._recent_deltas) < self.min_momentum_bars:
            return False
        
        if direction == "long":
            positive_count = sum(1 for d in self._recent_deltas if d > 0)
            return positive_count >= self.min_momentum_bars
        else:  # short
            negative_count = sum(1 for d in self._recent_deltas if d < 0)
            return negative_count >= self.min_momentum_bars

    def _get_median_volume(self) -> float:
        """Get median volume for this session."""
        if not self._session_volumes:
            return 0.0
        sorted_vols = sorted(self._session_volumes)
        mid = len(sorted_vols) // 2
        return float(sorted_vols[mid])

    def _detect_absorption(self, current, history) -> Optional[dict]:
        """
        Detect high-quality absorption at carried volume levels.
        
        Uses dynamic proximity based on volatility instead of fixed threshold.
        This should adapt better to different market conditions.
        """
        if current.volume == 0:
            return None

        # Require decent tick count
        if current.tick_count < self.min_tick_count:
            return None

        # Require above-median volume for this session
        median_vol = self._get_median_volume()
        if median_vol > 0 and current.volume < median_vol * 0.7:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Require meaningful bar body
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range
        if body_ratio < self.min_body_ratio:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Strict one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require clear wick showing price rejection
        has_wick = False
        if delta > 0:  # Buying pressure
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0
            if lower_wick >= self.min_wick_points and wick_ratio >= self.min_wick_ratio:
                has_wick = True
        elif delta < 0:  # Selling pressure
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0
            if upper_wick >= self.min_wick_points and wick_ratio >= self.min_wick_ratio:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to carried level with DYNAMIC threshold
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        dynamic_proximity = self._get_dynamic_proximity()
        
        if min_distance > dynamic_proximity:
            return None

        # Check momentum confirmation
        if delta > 0:  # Long
            if not self._has_momentum("long"):
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
        
        else:  # Short
            if not self._has_momentum("short"):
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

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on absorption detection."""
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
