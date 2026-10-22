"""
Volume profile absorption strategy - focus on directional confirmation without initiation delay.

Analysis of campaign history and synthetic test results:
- #16 (score -0.1076, 463 trades): Pure absorption, best score
- #43 (current, identical trades to baseline): Initiation filter not firing on synthetic data
- The synthetic identical trades warning suggests the initiation logic never activates

Key insight: The initiation filter (efficiency + body size checks) is likely never
triggering because the conditions are too strict or the synthetic data doesn't produce
bars that meet them. This means we're trading absorption immediately anyway, but with
added complexity that doesn't help.

The core issue: We're trying to improve selectivity through post-absorption confirmation,
but the campaign asks about absorption itself - whether it precedes moves. Adding filters
after detection may be filtering out real signals.

New approach: Return to pure absorption detection but with critical improvements:

1. **Better level persistence**: Don't reset levels at session boundary - accumulate
   across all prior sessions seen, not just last 2-3. This matches the campaign's
   emphasis on "carried levels from prior sessions."

2. **Simpler absorption criteria**: Remove the wick requirement entirely. Absorption
   is defined by strong directional flow AT a level, not by specific bar shape.
   The campaign says "flow that fails to move price" - that's the delta_ratio alone.

3. **Proximity to level is the key filter**: The campaign emphasizes "price arrives at
   a carried level" - make this the primary selection criterion. Proximity threshold
   of 1.0 point is the real selectivity.

4. **Realistic target sizing**: Use 2.0x stop distance for genuine level-to-level moves
   (the campaign asks about moves between levels, not quick scalps).

5. **No initiation complexity**: The synthetic test shows initiation isn't changing
   trades, so it's not the issue. Go back to immediate entry on absorption detection.

The reason #16 worked: It was pure absorption (delta + wick + level proximity) with
463 trades. We should maintain that selectivity but ensure the absorption detection
is firing consistently.

If synthetic test still shows identical trades, the absorption detection itself may
never fire on that data (which is fine - synthetic data is noise). The real test is
on the scored data where #16 got -0.1076.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.45,
        level_proximity: float = 1.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.0,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Persistent level tracking across all sessions
        self._all_session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        
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

        # Handle session boundary - persist all prior session levels
        if current.session != self._current_session:
            self._on_session_change()
            self._current_session = current.session

        # Accumulate volume profile for current session
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption signal - pure version
        absorption = self._detect_absorption(current)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Move current session profile to history, reset for new session."""
        if self._current_session_profile:
            self._all_session_profiles.append(self._current_session_profile)
        
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels in current session."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Use 0.25-point granularity
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

    def _extract_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Get top levels comprising ~70% of volume
        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.70:
                break

        return sorted(list(set(top_levels)))

    def _get_all_carried_levels(self) -> list[float]:
        """Get all carried levels from all prior sessions and current session."""
        all_levels = []
        
        # Add levels from all completed sessions
        for profile in self._all_session_profiles:
            levels = self._extract_levels_from_profile(profile)
            all_levels.extend(levels)
        
        # Add levels from current session in progress
        current_levels = self._extract_levels_from_profile(self._current_session_profile)
        all_levels.extend(current_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong directional flow at a carried level.
        
        Absorption definition from campaign:
        - Strong one-sided flow (delta_ratio >= threshold)
        - Price at a carried level (proximity <= threshold)
        
        Simplified: remove wick requirement. The campaign defines absorption
        as "flow that fails to move price" - that's captured by delta alone,
        not bar shape.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Require strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check proximity to carried level - this is the real selectivity
        carried_levels = self._get_all_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Calculate stop and target based on bar extremes
        if delta > 0:  # Long setup
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # Short setup
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
