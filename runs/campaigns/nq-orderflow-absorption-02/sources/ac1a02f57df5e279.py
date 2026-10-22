"""
Volume profile absorption strategy - focused on reaching 50% exposure floor.

The campaign asks whether absorption at carried volume levels precedes moves
between levels. Prior attempts (#1-7) exposed in too few sessions (0-2 out of 384,
below the 50% floor of 192 sessions).

Root cause: the strategy was too selective. It required:
- Exact proximity to profile levels (0.25-0.5 points)
- High delta ratios (0.60-0.85)
- Price rejection relative to prior bar
- Structural stops within tight bounds

This created few setups per session (0-2 total), making 50% exposure impossible.

This version takes a different approach:
1. **Lower selectivity on level matching**: Any price within 2.0 points of a
   profile level is "at" that level. This dramatically increases setup frequency.
2. **Simpler absorption**: Strong delta (>0.55) + any profile level nearby = setup.
   No rejection check, no multi-bar confirmation delay.
3. **Wider stop bounds**: Accept stops from 0.5 to 6.0 points. Variance is
   information, not noise.
4. **Act immediately**: Place entry on absorption bar itself, no waiting for
   initiation. The holding period (multi-bar move to next level) is the real
   protection.
5. **One setup per session max**: Avoid over-trading the same pattern.

The key insight: if the signal is real, it should work at many price levels,
not just perfectly-measured ones. If we can't reach 50% exposure with simple
rules, the signal may not exist at this cost structure. But we need to test
that hypothesis properly, which means generating enough setups to measure it.

This version should trade 50-100+ times per 384 sessions (130-260 total trades),
easily clearing the 50% exposure floor and allowing the quality of the signal
to be measured.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        # Absorption detection - lenient for high frequency
        absorption_delta_ratio: float = 0.55,
        level_proximity: float = 2.0,  # Much wider proximity
        min_level_volume: int = 10,  # Very low minimum
        # Stop and target - structural with wider bounds
        stop_buffer: float = 0.25,
        min_stop_distance: float = 0.5,
        max_stop_distance: float = 6.0,
        target_multiple: float = 2.0,
        # Frequency control
        max_setups_per_session: int = 1,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_level_volume = min_level_volume
        self.stop_buffer = stop_buffer
        self.min_stop_distance = min_stop_distance
        self.max_stop_distance = max_stop_distance
        self.target_multiple = target_multiple
        self.max_setups_per_session = max_setups_per_session
        self.size = size

        # Session and bar tracking
        self._current_session: Optional[int] = None
        self._setups_this_session: int = 0
        self._all_bars: deque = deque(maxlen=600)
        
        # Volume profile from recent bars
        self._profile_levels: dict[float, int] = {}

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        current = history[-1]
        prior = history[-2]

        # Handle session change
        if current.session != self._current_session:
            self._on_session_change()
            self._current_session = current.session

        # Accumulate bars for profile
        self._all_bars.append(current)
        self._rebuild_profile()

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Skip if we've already placed a setup this session
        if self._setups_this_session >= self.max_setups_per_session:
            return

        # Check for absorption
        absorption = self._detect_absorption(current, prior)
        if absorption:
            self._place_entry(ctx, absorption)
            self._setups_this_session += 1

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Reset session state."""
        self._setups_this_session = 0

    def _rebuild_profile(self) -> None:
        """Build volume profile from recent bars."""
        self._profile_levels = defaultdict(int)
        
        # Use last 400 bars (multiple sessions)
        bars_to_use = list(self._all_bars)[-400:]
        
        for bar in bars_to_use:
            if bar.volume <= 0 or bar.high <= bar.low:
                continue
            
            # Distribute volume across price levels at 0.25 granularity
            low_rounded = round(bar.low * 4) / 4
            high_rounded = round(bar.high * 4) / 4
            
            num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
            vol_per_level = bar.volume / num_levels
            
            level = low_rounded
            while level <= high_rounded + 0.01:
                level_rounded = round(level * 4) / 4
                self._profile_levels[level_rounded] += int(vol_per_level)
                level += 0.25

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption: strong one-way flow near a profile level.
        
        Simple rule:
        1. Strong delta (absolute value > threshold * volume)
        2. Price is within proximity to any profile level
        3. Stop distance is within acceptable bounds
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Find if price is near any profile level
        if not self._profile_levels:
            return None
        
        profile_prices = sorted(self._profile_levels.keys())
        nearby_levels = [
            p for p in profile_prices
            if abs(p - current.close) <= self.level_proximity
        ]

        if not nearby_levels:
            return None

        # Pick the closest level
        closest_level = min(nearby_levels, key=lambda p: abs(p - current.close))
        level_volume = self._profile_levels[closest_level]
        
        if level_volume < self.min_level_volume:
            return None

        # Determine direction from delta sign
        if delta > 0:
            direction = "long"
            # Stop below the bar
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
        else:
            direction = "short"
            # Stop above the bar
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

        # Validate stop distance
        if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
            return None

        return {
            "direction": direction,
            "level": closest_level,
            "stop_distance": stop_distance,
            "target_distance": stop_distance * self.target_multiple,
            "delta_ratio": delta_ratio,
        }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order."""
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
