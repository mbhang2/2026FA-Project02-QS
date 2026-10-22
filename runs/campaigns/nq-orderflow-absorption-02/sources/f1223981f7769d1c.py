"""
Volume profile absorption strategy - test if initiation confirmation is the key.

Analysis of the campaign:
- #16 scored -0.1076 with 463 trades (immediate absorption entry)
- All recent trials produce identical synthetic trades (70)
- The identical synthetic trades persist even with initiation logic added

Key insight: The synthetic identical trades at 70 might indicate that the synthetic
baseline itself doesn't have real absorption signals, OR our detection is still
matching it when we do fire.

New hypothesis: The issue might be that we're detecting absorption but the
initiation check is too strict, so we never actually enter on real data.

Or: The initiation logic is correct but we need to be more selective about
which absorption bars we wait to confirm. Not every strong delta bar with a wick
deserves a confirmation wait.

Diagnostic approach: 
1. Make absorption detection MORE selective (higher thresholds)
2. Make initiation confirmation MORE lenient (easier to pass)
3. This way: fewer absorption bars detected, but more of them confirm

This tests whether the problem is:
- False absorption detections (too many bars flagged)
- Or overly strict confirmation (real moves rejected)

If we get different synthetic trades: the detection selectivity matters
If we get same synthetic but better real score: the confirmation balance was wrong
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.50,  # Stricter: only extreme absorption
        min_wick_ratio: float = 0.15,  # Stricter: significant wick required
        level_proximity: float = 0.15,  # Stricter: very close to level
        efficiency_percentile: float = 0.50,  # Lenient: only need 50th percentile
        body_percentile: float = 0.40,  # Lenient: lower body requirement
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        efficiency_window: int = 20,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_ratio = min_wick_ratio
        self.level_proximity = level_proximity
        self.efficiency_percentile = efficiency_percentile
        self.body_percentile = body_percentile
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.efficiency_window = efficiency_window

        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Current session volume profile
        self._session_profile: dict[float, int] = {}
        
        # Absorption detection state
        self._absorption_bar: Optional[dict] = None
        self._absorption_bar_ts: Optional[int] = None
        
        # Efficiency tracking for recent bars
        self._recent_efficiencies: deque = deque(maxlen=efficiency_window)
        self._recent_bodies: deque = deque(maxlen=efficiency_window)

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
            self._session_profile = {}
            self._absorption_bar = None
            self._absorption_bar_ts = None
            self._recent_efficiencies.clear()
            self._recent_bodies.clear()

        # Accumulate volume profile
        self._update_profile(current)

        # Track efficiency metrics for this bar
        self._update_efficiency_metrics(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Check if we have a pending absorption bar waiting for confirmation
        if self._absorption_bar is not None:
            # Check if current bar is an initiating bar
            initiation = self._check_initiation(current, self._absorption_bar)
            if initiation:
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, initiation, "absorption-confirmed")
                self._absorption_bar = None
                self._absorption_bar_ts = None
            elif current.close_ts != self._absorption_bar_ts:
                # Only wait one bar for confirmation, then reset
                self._absorption_bar = None
                self._absorption_bar_ts = None

        # Detect new absorption bar (only if not waiting for confirmation)
        if self._absorption_bar is None:
            absorption = self._detect_absorption(current)
            if absorption:
                self._absorption_bar = absorption
                self._absorption_bar_ts = current.close_ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_profile(self, bar) -> None:
        """Accumulate volume at price levels with 0.25-point granularity."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._session_profile[level_key] = self._session_profile.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _update_efficiency_metrics(self, bar) -> None:
        """Track efficiency and body size for recent bars."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Efficiency: points traveled / absolute signed flow
        abs_delta = abs(bar.delta)
        if abs_delta > 0:
            points_traveled = bar.high - bar.low
            efficiency = points_traveled / abs_delta
            self._recent_efficiencies.append(efficiency)
        else:
            self._recent_efficiencies.append(0.0)

        # Body size
        body = abs(bar.close - bar.open)
        self._recent_bodies.append(body)

    def _get_peak_levels(self) -> list[float]:
        """Extract peak volume levels from current session profile."""
        if not self._session_profile:
            return []

        sorted_items = sorted(self._session_profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Get levels comprising top 60% of volume
        peak_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            peak_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.60:
                break

        return sorted(list(set(peak_levels)))

    def _is_near_level(self, price: float, levels: list[float]) -> bool:
        """Check if price is near a peak volume level."""
        if not levels:
            return False
        min_distance = min(abs(price - level) for level in levels)
        return min_distance <= self.level_proximity

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: STRICTER thresholds to reduce false signals.
        
        Only the most obvious absorption bars get flagged for confirmation.
        This reduces the number of bars we wait to confirm, but increases
        the quality of the ones we do.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Stricter: 50% threshold - only extreme one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Stricter: 15% wick - significant failed extension
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

        # Stricter: 0.15 points to level - very close proximity
        peak_levels = self._get_peak_levels()
        if not self._is_near_level(current.close, peak_levels):
            return None

        # Calculate stop and target
        if delta > 0:  # Long absorption
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "absorption_price": current.close,
                "absorption_high": current.high,
                "absorption_low": current.low,
                "stop_distance": stop_distance,
            }
        
        else:  # Short absorption
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "absorption_price": current.close,
                "absorption_high": current.high,
                "absorption_low": current.low,
                "stop_distance": stop_distance,
            }

    def _check_initiation(self, current, absorption_bar) -> Optional[dict]:
        """
        Check if current bar is initiating after absorption.
        
        LENIENT thresholds to confirm more of the absorption bars we detect:
        - 50th percentile efficiency (vs 75th before)
        - 40th percentile body (vs 50th before)
        
        This increases confirmation rate while keeping absorption detection strict.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        direction = absorption_bar["direction"]
        delta = current.delta

        # Flow must continue in absorption direction
        if direction == "long" and delta <= 0:
            return None
        if direction == "short" and delta >= 0:
            return None

        # Calculate current bar's efficiency
        abs_delta = abs(delta)
        if abs_delta > 0:
            points_traveled = current.high - current.low
            current_efficiency = points_traveled / abs_delta
        else:
            return None

        # Lenient: 50th percentile efficiency
        if len(self._recent_efficiencies) >= 3:
            sorted_eff = sorted(self._recent_efficiencies)
            threshold_idx = int(len(sorted_eff) * self.efficiency_percentile)
            efficiency_threshold = sorted_eff[min(threshold_idx, len(sorted_eff) - 1)]
            if current_efficiency < efficiency_threshold:
                return None

        # Lenient: 40th percentile body
        body = abs(current.close - current.open)
        if len(self._recent_bodies) >= 3:
            sorted_bodies = sorted(self._recent_bodies)
            body_threshold_idx = int(len(sorted_bodies) * self.body_percentile)
            body_threshold = sorted_bodies[min(body_threshold_idx, len(sorted_bodies) - 1)]
            if body < body_threshold:
                return None

        # Confirmed initiation
        stop_distance = absorption_bar["stop_distance"]
        
        return {
            "direction": direction,
            "stop_distance": stop_distance,
            "target_distance": stop_distance * self.target_multiple,
        }

    def _place_entry(self, ctx, setup: dict, tag: str) -> None:
        """Place entry order on confirmed initiation."""
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
