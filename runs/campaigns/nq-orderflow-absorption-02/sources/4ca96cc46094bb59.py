"""
Volume profile absorption strategy - refined toward #81's exceptional performance.

Campaign analysis of best performers:
- #81: -0.0096 with 622 trades (EXCEPTIONAL: 10x better than #76)
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades
- #86: -0.0600 with 673 trades

Critical insight: The synthetic test showed IDENTICAL 70 trades, meaning filters
aren't reducing trade count there. But on real data, #81 trades only 622 times
across 384 sessions (~1.6/session), while we're trading more frequently.

Key realization from #81's success: It's not just stricter parameters, it's a
fundamentally different approach to what counts as "absorption at a level."

The current strategy extracts top 2 levels per session, but this may be:
1. Too few levels early in a session (missing real structure)
2. Too loose on what "top 2" means (volume-weighted, but maybe not robust)
3. Not properly handling the multi-session level carrying

This trial takes a different approach:
- Extract top 4-5 levels per session (more robust reference set)
- Use stricter delta_ratio (0.36) but allow it to work on more levels
- Tighten wick requirement to 0.55 (still strong but more achievable)
- Tighten level_proximity to 1.5 (very close to actual levels)
- Use target_multiple of 2.0 (balanced risk/reward)
- Reduce max_stop_distance to 2.2 (tighter stops)
- Add a minimum volume requirement: bar must have >500 contracts

The hypothesis: #81 succeeds by having a better-calibrated reference set
(more levels but higher quality) and stricter signal quality (delta, wick,
volume). This should produce ~400-600 trades on real data with better quality.

Expected: Score near #81's -0.0096, with 450-650 trades on real data.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.36,
        level_proximity: float = 1.5,
        min_wick_points: float = 0.55,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.0,
        max_stop_distance: float = 2.2,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_levels_per_session: int = 5,
        min_bar_volume: int = 500,
        min_bar_body: float = 0.35,
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
        self.max_levels_per_session = max_levels_per_session
        self.min_bar_volume = min_bar_volume
        self.min_bar_body = min_bar_body

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
            # Keep only last 2 sessions
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Distribute volume across 0.25-point granularity
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
        """Extract the strongest levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)

        # Keep top 4-5 most significant levels (robust reference set)
        top_levels = [level for level, vol in sorted_items[:self.max_levels_per_session]]

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        
        # Current session levels
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        
        # Prior session levels (up to 2 sessions back)
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption at carried volume levels.
        
        Calibrated criteria for #81-level performance:
        - Strong one-sided flow (delta_ratio >= 0.36)
        - Pronounced price rejection (wick >= 0.55)
        - Tight proximity to carried levels (1.5 points)
        - Clear directional bar body (>= 0.35 of range)
        - Minimum bar volume (>= 500 contracts)
        - Conservative targets (2.0x stop distance)
        - Tight stops (max 2.2 points)
        """
        if current.volume == 0:
            return None

        # Require minimum volume
        if current.volume < self.min_bar_volume:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Strict delta ratio threshold
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require pronounced wick showing price rejection
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

        # Require clear directional body
        bar_range = current.high - current.low
        if bar_range <= 0:
            return None
        
        if delta > 0:  # Long
            body = current.close - current.open
            if body / bar_range < self.min_bar_body:
                return None
        else:  # Short
            body = current.open - current.close
            if body / bar_range < self.min_bar_body:
                return None

        # Check proximity to carried levels
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
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
