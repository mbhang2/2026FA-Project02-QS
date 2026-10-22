"""
Volume profile absorption strategy with refined signal detection.

Prior attempts exposed in 0-2 sessions, well below the 50% floor. The synthetic test
showed identical trades to baseline, meaning absorption detection wasn't firing.

Root cause: The absorption detection logic had several issues:
1. Profile building only used current session bars - not enough history early in session
2. Level proximity check required price to be WITHIN 1.0 of a level - but price approaches
   levels from distance, so we need to check if price is ARRIVING AT a level
3. Delta ratio threshold (0.60) combined with strict level proximity meant few setups fired
4. The bar_key deduplication was based on timestamps, but we need to avoid re-entering
   on the same bar index in history

Key changes to increase exposure:

1. **Build profile from training data in setup()**: Use ctx.train_bars() to establish
   baseline volume levels from the entire training split. This gives us strong reference
   levels that persist across the scored sessions.

2. **Track price arrival at levels**: Instead of checking if price is within 1.0 of a level,
   check if price is MOVING TOWARD or AT a level. A bar that closes near a level after
   strong directional flow is absorbing at that level.

3. **Lower delta threshold to 0.55**: This catches more genuine absorption while still
   requiring directional flow.

4. **Better deduplication**: Track the bar index in history, not timestamps, to avoid
   entering twice on the same bar.

5. **Accumulate session profile incrementally**: Build profile from bars as they arrive,
   not just at session end. This gives us usable levels throughout the session.

6. **Simpler absorption logic**: Absorption = (1) price near a known level, (2) strong
   one-way delta, (3) stop distance in valid range. No need for complex rejection patterns.

7. **Immediate entry**: Place entry immediately when absorption is detected. The 2.5x
   target and structural stop handle risk/reward.

The goal: increase exposure from 0-2 sessions to 100+ sessions (26%+) by having more
reliable level detection and simpler entry logic.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.55,
        level_proximity: float = 1.5,
        stop_buffer: float = 0.5,
        target_multiple: float = 2.5,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.5,
        price_granularity: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.price_granularity = price_granularity
        self.size = size

        # Session tracking
        self._current_session: Optional[int] = None
        self._bars_in_session: list = []
        self._accumulated_profile: dict[float, int] = {}

        # Baseline levels from training data
        self._baseline_levels: list[float] = []

        # Prior session levels
        self._prior_levels: list[float] = []

        # Track last bar index we entered on (to avoid double-entry on same bar)
        self._last_entry_bar_index: int = -1000

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Build baseline levels from training split
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars and len(train_bars) > 0:
                self._baseline_levels = self._extract_levels_from_bars(train_bars)
        except Exception:
            # If train_bars fails, continue without baseline
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
            self._on_session_change(current)
            self._current_session = current.session

        # Accumulate bars in current session
        self._bars_in_session.append(current)

        # Update accumulated profile with this bar's volume
        self._update_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check for absorption setup on this bar
        # Use history index to avoid entering twice on the same bar
        current_bar_index = len(history) - 1
        if current_bar_index != self._last_entry_bar_index and len(history) >= 2:
            absorption = self._detect_absorption(current, history[-2])
            if absorption:
                self._last_entry_bar_index = current_bar_index
                self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, bar) -> None:
        """Extract levels from prior session and prepare for new one."""
        if self._accumulated_profile:
            self._prior_levels = self._extract_levels(self._accumulated_profile)

        self._bars_in_session = []
        self._accumulated_profile = {}

    def _extract_levels_from_bars(self, bars) -> list[float]:
        """Extract significant levels from a sequence of bars."""
        profile: dict[float, int] = defaultdict(int)
        
        for bar in bars:
            if bar.volume <= 0 or bar.high <= bar.low:
                continue
            
            low_bucket = round(bar.low / self.price_granularity) * self.price_granularity
            high_bucket = round(bar.high / self.price_granularity) * self.price_granularity
            
            num_buckets = max(1, int((high_bucket - low_bucket) / self.price_granularity) + 1)
            vol_per_bucket = bar.volume / num_buckets
            
            level = low_bucket
            while level <= high_bucket + 0.001:
                level_rounded = round(level / self.price_granularity) * self.price_granularity
                profile[level_rounded] += int(vol_per_bucket)
                level += self.price_granularity
        
        return self._extract_levels(profile)

    def _update_profile(self, bar) -> None:
        """Accumulate volume at price levels throughout the session."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        low_bucket = round(bar.low / self.price_granularity) * self.price_granularity
        high_bucket = round(bar.high / self.price_granularity) * self.price_granularity

        num_buckets = max(1, int((high_bucket - low_bucket) / self.price_granularity) + 1)
        vol_per_bucket = bar.volume / num_buckets

        level = low_bucket
        while level <= high_bucket + 0.001:
            level_rounded = round(level / self.price_granularity) * self.price_granularity
            self._accumulated_profile[level_rounded] = self._accumulated_profile.get(level_rounded, 0) + int(vol_per_bucket)
            level += self.price_granularity

    def _extract_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract significant price levels from a profile."""
        if not profile:
            return []

        sorted_levels = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_levels)

        # Get top levels that comprise ~70% of volume
        top_levels = []
        cumulative = 0
        for level, vol in sorted_levels[:15]:
            top_levels.append(level)
            cumulative += vol
            if cumulative > total_vol * 0.70:
                break

        return sorted(top_levels)

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption: strong one-way delta at a price level.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Build candidate levels from multiple sources
        candidate_levels = (
            list(self._accumulated_profile.keys()) +
            self._prior_levels +
            self._baseline_levels
        )

        # Remove duplicates
        candidate_levels = list(set(candidate_levels))

        # Check if current price is near any carried level
        near_level = False
        closest_level = None
        min_distance = float('inf')

        for level in candidate_levels:
            distance = abs(current.close - level)
            if distance <= self.level_proximity and distance < min_distance:
                near_level = True
                closest_level = level
                min_distance = distance

        if not near_level or closest_level is None:
            return None

        # Determine direction and calculate stop
        if delta > 0:  # Buying pressure
            # Long absorption at level
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "level": closest_level,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        elif delta < 0:  # Selling pressure
            # Short absorption at level
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "level": closest_level,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        return None

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry with structural stop and target."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        tag = "absorption-entry"

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
