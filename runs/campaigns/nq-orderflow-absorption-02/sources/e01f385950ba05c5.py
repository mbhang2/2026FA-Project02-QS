"""
Volume profile absorption strategy - test if the synthetic identical trades are due to
our level proximity being too loose and accidentally matching momentum patterns.

The core problem: 70 identical synthetic trades across many parameter changes suggests
our absorption detection is firing on the same bars as the baseline momentum strategy.

Key insight from trial history:
- #16 scored -0.1076 with 463 trades (best result so far)
- #13 scored -0.9744 with 102,546 trades (pure momentum)
- Multiple trials with identical synthetic trades (70) despite parameter changes

The 70 identical synthetic trades are the diagnostic signal. If our absorption detection
produces the same 70 trades as the baseline on synthetic data, it means either:
1. Our level proximity is so loose it matches everything (currently 0.25 points)
2. The delta + wick pattern alone is matching momentum bars
3. We're not filtering enough on the actual absorption signal

New hypothesis: The synthetic baseline trades momentum (close > open), and our absorption
detection with loose level proximity accidentally matches those same bars because:
- Momentum bars have strong directional flow (high delta)
- Momentum bars often have wicks (failed extension in prior direction)
- With level_proximity=0.25, almost any bar matches some level

Solution: Make level proximity MUCH tighter and require the level to be truly significant:
1. **Increase level_proximity to 1.5 points** - must be very close to a major level
2. **Require level to be in top 30% of volume** - only strongest levels matter
3. **Higher delta ratio 0.50** - only extreme absorption
4. **Keep wick ratio at 0.30** - significant failed extension
5. **Lower target multiple to 1.5** - conservative move targets
6. **Accumulate levels but filter strictly** - only use levels with high volume

The key difference: instead of matching any level within 0.25 points, we now require
proximity to a genuinely significant level. This should prevent matching momentum bars
that happen to be near any price level.

If this still produces 70 identical trades on synthetic, then the baseline IS using
a similar absorption-based approach and we need a fundamentally different signal.
If we get different trades, we've found and fixed the blocker.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback_bars: int = 20,
        absorption_delta_ratio: float = 0.50,
        level_proximity: float = 1.5,
        min_wick_ratio: float = 0.30,
        stop_buffer: float = 0.25,
        target_multiple: float = 1.5,
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
        
        # Accumulate high-volume levels across sessions
        self._all_high_volume_levels: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Extract levels from training split
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars:
                profile = self._build_volume_profile(train_bars)
                self._all_high_volume_levels = self._get_high_volume_levels(profile)
        except Exception:
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
            self._on_session_change(history)
            self._current_session = current.session

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

        # Detect absorption
        absorption = self._detect_absorption(current, volume_profile)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, history) -> None:
        """Extract and accumulate levels at session boundary."""
        # Get recent bars from previous session
        recent = history.last(min(self.lookback_bars, len(history)))
        if recent:
            profile = self._build_volume_profile(recent)
            levels = self._get_high_volume_levels(profile)
            self._all_high_volume_levels.extend(levels)
            # Keep only unique levels
            self._all_high_volume_levels = sorted(
                list(set(self._all_high_volume_levels))
            )
        
        self._entered_bar_timestamps = set()

    def _build_volume_profile(self, bars) -> dict[float, int]:
        """Build volume profile from bars with 0.25-point granularity."""
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
        """Extract high-volume levels from profile (top 30% of volume)."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.30:
                break

        return sorted(list(set(top_levels)))

    def _get_all_carried_levels(self) -> list[float]:
        """Get all carried levels from current session and prior sessions."""
        return self._all_high_volume_levels if self._all_high_volume_levels else []

    def _detect_absorption(
        self, current, volume_profile: dict[float, int]
    ) -> Optional[dict]:
        """
        Detect absorption: strong one-sided flow at significant high-volume level with failed extension.
        
        Much stricter criteria to avoid matching momentum patterns:
        - Higher delta ratio (0.50) - only extreme absorption
        - Much tighter level proximity (1.5 points) - must be very close to major level
        - Higher wick ratio (0.30) - significant failed extension
        - Levels must be in top 30% of volume - only truly significant levels
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Extreme one-sided flow (very strict threshold)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Wick opposite to flow direction (strict threshold)
        has_significant_wick = False
        if delta > 0:
            lower_wick = current.close - current.low
            if lower_wick / range_size >= self.min_wick_ratio:
                has_significant_wick = True
        elif delta < 0:
            upper_wick = current.high - current.close
            if upper_wick / range_size >= self.min_wick_ratio:
                has_significant_wick = True

        if not has_significant_wick:
            return None

        # Price near MAJOR high-volume level (much stricter proximity)
        current_session_levels = self._get_high_volume_levels(volume_profile)
        all_levels = list(set(current_session_levels + self._get_all_carried_levels()))
        
        if not all_levels:
            return None

        min_distance = min(abs(current.close - level) for level in all_levels)
        if min_distance > self.level_proximity:
            return None

        # Calculate stop and target
        if delta > 0:  # Long
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
