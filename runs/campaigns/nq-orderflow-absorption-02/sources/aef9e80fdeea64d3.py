"""
Volume profile absorption strategy - simplify to core absorption signal.

Analysis of trial history shows:
- #16 scored -0.1076 with 463 trades (pure absorption)
- #13 scored -0.9744 with 102,546 trades (momentum fallback)
- 47+ trials with identical synthetic trades suggest we're matching baseline behavior

The synthetic identical trades (70 trades) across many parameter changes is the key insight.
This means either:
1. The baseline strategy IS absorption-based (delta + wick pattern)
2. Our absorption detection is accidentally equivalent to baseline momentum
3. The synthetic data lacks the conditions needed for absorption

The fact that #16 scored (even if negative) suggests absorption CAN work on real data.
The identical synthetic trades suggest we're not implementing it in a way that differs
from the baseline on synthetic data.

New approach: Go back to basics and make absorption detection MORE ROBUST by:
1. **Remove initiation complexity** - it added logic without improving #16's score
2. **Focus on absorption as defined**: strong flow at a level with failed extension
3. **Use bar.delta directly** - it's the actual order flow
4. **Require proximity to HIGH VOLUME level** - not just any level
5. **Keep structural stop** - just beyond bar extreme + buffer
6. **Simpler efficiency check** - only if efficiency is well above baseline

The key difference from baseline momentum:
- Momentum: close > open (simple price direction)
- Absorption: strong delta + wick opposite to flow + at high-volume level (flow + price + level)

If this still produces identical trades on synthetic, the baseline IS absorption-based.
If different, we've finally separated from it.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback_bars: int = 20,
        absorption_delta_ratio: float = 0.40,
        level_proximity: float = 0.5,
        min_wick_ratio: float = 0.20,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback_bars = lookback_bars
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Track bars we've entered on
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

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Build volume profile from recent bars
        recent_bars = history.last(min(self.lookback_bars, len(history)))
        if not recent_bars:
            return

        volume_profile = self._build_volume_profile(recent_bars)
        if not volume_profile:
            return

        # Detect absorption: strong delta at high-volume level with wick
        absorption = self._detect_absorption(current, volume_profile)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _build_volume_profile(self, bars) -> dict[float, int]:
        """Build volume profile from recent bars with 0.25-point granularity."""
        profile: dict[float, int] = defaultdict(int)

        for bar in bars:
            if bar.volume <= 0 or bar.high <= bar.low:
                continue

            low_rounded = round(bar.low * 4) / 4
            high_rounded = round(bar.high * 4) / 4

            num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
            vol_per_level = bar.volume / num_levels

            level = low_rounded
            while level <= high_rounded + 0.01:
                level_key = round(level * 4) / 4
                profile[level_key] += int(vol_per_level)
                level += 0.25

        return dict(profile)

    def _get_high_volume_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract high-volume levels from profile (top 50% of volume)."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Get levels comprising top 50% of volume
        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.50:
                break

        return sorted(list(set(top_levels)))

    def _detect_absorption(
        self, current, volume_profile: dict[float, int]
    ) -> Optional[dict]:
        """
        Detect absorption: strong one-sided flow at a high-volume level with failed extension.

        Core markers:
        1. Strong one-sided flow (delta_ratio >= 0.40)
        2. Wick opposite to flow direction (>= 20% of bar range)
        3. Price within level_proximity of a high-volume level
        4. Stop and target are within acceptable ranges
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Marker 1: Strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Marker 2: Wick opposite to flow direction
        has_significant_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick / range_size >= self.min_wick_ratio:
                has_significant_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick / range_size >= self.min_wick_ratio:
                has_significant_wick = True

        if not has_significant_wick:
            return None

        # Marker 3: Price near a high-volume level
        high_vol_levels = self._get_high_volume_levels(volume_profile)
        if not high_vol_levels:
            return None

        min_distance = min(abs(current.close - level) for level in high_vol_levels)
        if min_distance > self.level_proximity:
            return None

        # Calculate stop and target
        if delta > 0:  # Long absorption
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        else:  # Short absorption
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
