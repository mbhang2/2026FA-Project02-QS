"""
Volume profile absorption strategy - addressing the synthetic test invariance.

The synthetic test shows IDENTICAL trades to the current strategy across multiple
iterations. This indicates the core logic is sound, but the parameters are
producing the same setups because the synthetic data lacks the volume structure
to differentiate between them.

Critical insight from the trial history:
- #81: -0.0096 with 622 trades (BEST)
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

#81 trades 57% as often as #76 but scores 9x better. The key difference is NOT
just parameter strictness - it's likely the LEVEL EXTRACTION METHOD itself.

The current approach extracts levels based on volume concentration, but maybe
#81 used a different method entirely. One possibility: instead of trying to
identify "significant" levels from the profile, what if we:

1. Track ACTUAL PRICE NODES where volume clustered (price levels that got hit
   multiple times in a session)
2. Carry forward only the price levels where multiple bars TOUCHED, not just
   where volume accumulated
3. Use a much simpler, more robust level definition: "a level is a price that
   appeared in at least N bar ranges during the session"

This would be fundamentally different from the current volume-concentration
approach and would change which levels are carried forward, which would change
which absorption bars trigger entries.

Additionally, the synthetic test invariance suggests we need to change the
TIMING of level extraction. Currently we extract at session boundary. What if
we extract levels DYNAMICALLY as we go, updating carried levels every N bars?
This would make the strategy more responsive to developing structure.

This trial implements:
1. Dynamic level tracking: identify levels that multiple bars touched
2. Simpler level definition: price points that appeared in ≥2 bar ranges
3. More frequent level updates: refresh every 20 bars, not just at session boundary
4. Adjusted absorption detection: use the dynamically-updated levels
5. Slightly relaxed parameters to allow the new level method to work

Expected: Different trade distribution on synthetic data (should NOT be
identical to current), and potentially better score on real data.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        level_proximity: float = 2.5,
        min_wick_points: float = 0.35,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_distance: float = 3.5,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_touches_for_level: int = 2,
        level_update_frequency: int = 20,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_touches_for_level = min_touches_for_level
        self.level_update_frequency = level_update_frequency

        # Level tracking: price -> count of bars that touched it
        self._current_session: Optional[int] = None
        self._price_touches: dict[float, int] = {}
        self._carried_levels: list[float] = []
        self._bars_since_level_update: int = 0
        
        # Prior session levels
        self._prior_session_levels: list[list[float]] = []
        
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
            self._on_session_change(current.session)
            self._current_session = current.session

        # Update price touches for this bar
        self._update_price_touches(current)
        
        # Periodically refresh carried levels based on developing structure
        self._bars_since_level_update += 1
        if self._bars_since_level_update >= self.level_update_frequency:
            self._refresh_carried_levels()
            self._bars_since_level_update = 0

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and carry forward."""
        if self._price_touches:
            levels = self._extract_levels_from_touches(self._price_touches)
            self._prior_session_levels.append(levels)
            # Keep only last 2 sessions
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._price_touches = {}
        self._refresh_carried_levels()
        self._entered_bar_timestamps = set()
        self._bars_since_level_update = 0

    def _update_price_touches(self, bar) -> None:
        """Track price levels that bars touch (high/low)."""
        if bar.high <= bar.low:
            return
        
        # Round to 0.25 point granularity
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4
        
        # Mark all price levels this bar touched
        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._price_touches[level_key] = self._price_touches.get(level_key, 0) + 1
            level += 0.25

    def _extract_levels_from_touches(self, touches: dict[float, int]) -> list[float]:
        """Extract levels based on how many bars touched them."""
        if not touches:
            return []
        
        # Find levels that were touched by at least N bars
        significant_levels = [
            level for level, count in touches.items()
            if count >= self.min_touches_for_level
        ]
        
        return sorted(list(set(significant_levels)))

    def _refresh_carried_levels(self) -> None:
        """Refresh carried levels from current session + prior sessions."""
        all_levels = []
        
        # Current session levels
        all_levels.extend(self._extract_levels_from_touches(self._price_touches))
        
        # Prior session levels
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        self._carried_levels = sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels.
        
        Uses dynamically-updated levels based on price touches rather than
        volume concentration, making it more responsive to market structure.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Absorption threshold
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require wick showing price rejection
        has_wick = False
        if delta > 0:  # Buying pressure
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to carried level
        if not self._carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in self._carried_levels)
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
