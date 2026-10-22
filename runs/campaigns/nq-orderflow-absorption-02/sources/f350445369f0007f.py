"""
Volume profile absorption strategy - exploring parameter space around #81's success.

Campaign analysis shows:
- #81: -0.0096 with 622 trades (BEST)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades

The previous trial produced byte-identical trades to synthetic baseline, meaning
the parameters are still not reaching the selectivity that #81 achieves.

Key insight: #81 trades 622 times with exceptional performance. The fact that
previous attempts with stricter parameters still produce identical trades suggests
we need to examine the STRUCTURE of the filtering, not just make numbers smaller.

Hypothesis: #81 may be using a different approach to level proximity or absorption
detection. Instead of just tightening all parameters uniformly, this trial:

1. Changes level_proximity logic to use PERCENTAGE distance from level (not absolute)
2. Adds a minimum bar body requirement (not just wick)
3. Refines wick_ratio calculation to be more selective
4. Reduces max_levels_per_session to 1 (only THE strongest node per session)
5. Increases min_tick_count to 6 (very liquid bars only)
6. Slightly relaxes delta_ratio to 0.38 (was 0.40) to allow more signal
7. Uses tighter stop_buffer of 0.15 instead of 0.25
8. Adds a bar_body_ratio requirement (body must be meaningful fraction of range)

The insight: #81's selectivity may come from structural requirements about bar
quality (body size, wick size relative to range) rather than just tighter absolute
thresholds. By requiring bars to have both a meaningful body AND a meaningful wick,
we filter for bars that show genuine absorption rather than just any bar with flow.

Expected: Different trade distribution that approaches #81's -0.0096 with 500-700 trades.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.38,
        level_proximity_pct: float = 0.08,
        min_wick_points: float = 0.50,
        min_wick_ratio: float = 0.35,
        min_body_ratio: float = 0.25,
        stop_buffer: float = 0.15,
        target_multiple: float = 2.1,
        max_stop_distance: float = 2.2,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_levels_per_session: int = 1,
        min_tick_count: int = 6,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity_pct = level_proximity_pct
        self.min_wick_points = min_wick_points
        self.min_wick_ratio = min_wick_ratio
        self.min_body_ratio = min_body_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.max_levels_per_session = max_levels_per_session
        self.min_tick_count = min_tick_count

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
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

        # Accumulate volume profile for current session
        self._update_session_profile(current)

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
        if self._session_levels:
            levels = self._extract_levels_from_profile(self._session_levels)
            self._prior_session_levels.append(levels)
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_bar_timestamps = set()

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
        """Extract only the most significant level from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        top_levels = [level for level, vol in sorted_items[:self.max_levels_per_session]]

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect high-quality absorption at carried volume levels."""
        if current.volume == 0:
            return None

        # Require high liquidity
        if current.tick_count < self.min_tick_count:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Require meaningful bar body
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range if bar_range > 0 else 0
        if body_ratio < self.min_body_ratio:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Strict one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require clear wick showing price rejection
        has_wick = False
        wick_ratio = 0.0
        
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0
            if lower_wick >= self.min_wick_points and wick_ratio >= self.min_wick_ratio:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0
            if upper_wick >= self.min_wick_points and wick_ratio >= self.min_wick_ratio:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to carried level using percentage distance
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        # Use percentage distance from current price
        proximity_pct = min_distance / current.close if current.close > 0 else float('inf')
        if proximity_pct > self.level_proximity_pct:
            return None

        # Calculate stop and target with strict validation
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
