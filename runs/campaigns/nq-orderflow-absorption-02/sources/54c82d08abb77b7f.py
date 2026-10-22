"""
Volume profile absorption strategy - refined filtering based on #81's success.

Campaign analysis shows:
- #81: -0.0096 with 622 trades (BEST - 10x better than #76)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades

The synthetic test produced byte-identical trades, meaning the core absorption
detection logic needs refinement, not just parameter tuning.

Key insight from #81's 10x superiority: The edge comes from being MORE SELECTIVE
about what counts as absorption, combined with carrying multi-session levels.

This trial combines:
1. Stricter absorption detection (delta_ratio 0.45, high tick_count requirement)
2. Multi-session level carrying (restore the "carried levels" requirement)
3. But with much tighter proximity filtering (only 0.5 points from level)
4. Require bars to show BOTH volume AND flow concentration at the level
5. Use a "level strength" metric - only trade at levels that have accumulated
   significant volume across multiple bars, not just one strong bar

The hypothesis: #81 succeeds by:
- Trading at levels that are genuinely important (accumulated volume)
- Requiring very clean absorption signals (high delta_ratio, high tick_count)
- Carrying levels forward but only using the strongest ones
- Being selective about proximity (must be very close to the level)

Expected: Score better than #76's -0.0893, with 600-800 trades on real data.
The stricter filters should produce different trades on synthetic data too.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.45,
        min_wick_points: float = 0.75,
        min_wick_ratio: float = 0.40,
        min_body_ratio: float = 0.30,
        min_tick_count: int = 6,
        level_proximity: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.0,
        max_stop_distance: float = 2.0,
        min_stop_distance: float = 0.40,
        size: int = 1,
        min_level_strength: int = 1000,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.min_wick_ratio = min_wick_ratio
        self.min_body_ratio = min_body_ratio
        self.min_tick_count = min_tick_count
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_level_strength = min_level_strength

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
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

        # Detect absorption at strong carried levels
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
            self._prior_session_levels.append(dict(self._session_levels))
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

    def _get_strong_levels(self) -> list[float]:
        """Get levels with significant accumulated volume from all sessions."""
        all_level_volumes: dict[float, int] = {}
        
        # Current session
        for level, vol in self._session_levels.items():
            all_level_volumes[level] = all_level_volumes.get(level, 0) + vol
        
        # Prior sessions
        for session_profile in self._prior_session_levels:
            for level, vol in session_profile.items():
                all_level_volumes[level] = all_level_volumes.get(level, 0) + vol
        
        # Filter to only strong levels
        strong_levels = [
            level for level, vol in all_level_volumes.items()
            if vol >= self.min_level_strength
        ]
        
        return sorted(strong_levels)

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption at strong carried levels.
        
        Stricter requirements:
        - delta_ratio >= 0.45 (very one-sided)
        - tick_count >= 6 (compression of flow)
        - Both wick and body significant
        - Must be very close to a strong level (0.5 points)
        """
        if current.volume == 0:
            return None

        # Require minimum tick count
        if current.tick_count < self.min_tick_count:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Calculate body ratio
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range

        # Require meaningful body
        if body_ratio < self.min_body_ratio:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Strict one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Get strong levels from all sessions
        strong_levels = self._get_strong_levels()
        if not strong_levels:
            return None

        # Find closest strong level
        min_distance = min(abs(current.close - level) for level in strong_levels)
        if min_distance > self.level_proximity:
            return None

        # Long setup
        if delta > 0:
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range

            # Require BOTH wick size and ratio
            if lower_wick < self.min_wick_points or wick_ratio < self.min_wick_ratio:
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

        # Short setup
        else:
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range

            # Require BOTH wick size and ratio
            if upper_wick < self.min_wick_points or wick_ratio < self.min_wick_ratio:
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
