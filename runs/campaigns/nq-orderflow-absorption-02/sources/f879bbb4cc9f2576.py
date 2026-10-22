"""
Volume profile absorption strategy - investigating level persistence and
multi-session structure as core to #81's exceptional performance.

The synthetic test produced byte-identical 70 trades, confirming core logic works.
However, all recent parameter tuning has produced identical trades on synthetic,
meaning the synthetic data doesn't stress-test the actual differentiators.

Analysis of top performers shows a clear pattern:
- #81: -0.0096 with 622 trades (EXCEPTIONAL - 10x better than #76)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades

The gap between #81 and others is not explained by minor parameter tweaks - 
it's a fundamental difference in signal quality. Key hypothesis:

#81's superior performance likely comes from:
1. **Level persistence weighting**: Levels that appear across multiple sessions
   are stronger signals than single-session levels. Carry them with higher weight.
2. **Stricter absorption definition**: Not just delta_ratio, but also requiring
   the bar to show structure consistent with true absorption (body vs wick ratio).
3. **Session-aware filtering**: Early session bars are noise; require bars deep
   enough into session for levels to have formed.
4. **Volume concentration**: Only trade at levels where volume is truly concentrated,
   not just distributed.

This trial implements persistent level weighting:
- Levels appearing in multiple sessions get higher priority
- Absorption detection weights proximity to persistent levels more heavily
- Requires bar to be in mid-to-late session before trading
- Uses confirmed absorption parameters: delta_ratio=0.36, proximity=0.75
- Conservative target multiple of 2.0 (proven by #81)
- Tighter stop distances (max 2.0) to improve win rate

The core insight: #81 likely trades fewer, higher-conviction setups by
preferring levels that have proven their strength across sessions, not
just in the current one.

Expected: Score near #81's -0.0096 with 500-650 trades on real data.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.36,
        level_proximity: float = 0.75,
        min_wick_points: float = 0.50,
        stop_buffer: float = 0.20,
        size: int = 1,
        max_stop_distance: float = 2.0,
        min_stop_distance: float = 0.20,
        top_levels_per_session: int = 4,
        min_tick_count: int = 3,
        min_bars_into_session: int = 3,
        target_multiple: float = 2.0,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.size = size
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.top_levels_per_session = top_levels_per_session
        self.min_tick_count = min_tick_count
        self.min_bars_into_session = min_bars_into_session
        self.target_multiple = target_multiple

        # Level tracking with persistence scoring
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []  # Store as dicts to track persistence
        self._bars_in_session: int = 0
        
        # Track entered bars
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
            self._bars_in_session = 0

        self._bars_in_session += 1

        # Accumulate volume profile
        self._update_session_profile(current)

        # Skip if in position or working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Skip early session bars - let profile develop and levels form
        if self._bars_in_session <= self.min_bars_into_session:
            return

        # Detect high-quality absorption at persistent levels
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract and carry forward levels from completed session."""
        if self._session_levels:
            self._prior_session_levels.append(self._session_levels.copy())
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        self._session_levels = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Use 0.25 point granularity for level identification
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        # Distribute volume proportionally across the range
        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _extract_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract only the strongest volume nodes."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        top_levels = [level for level, vol in sorted_items[:self.top_levels_per_session]]
        return sorted(list(set(top_levels)))

    def _get_carried_levels_with_persistence(self) -> list[tuple[float, float]]:
        """
        Get all carried levels with persistence scores.
        
        Returns list of (level, persistence_score) tuples.
        Persistence score = 1.0 for current session, 0.5 for prior sessions.
        Levels appearing in multiple sessions get higher scores.
        """
        level_scores: dict[float, float] = {}
        
        # Current session levels - highest weight
        current_levels = self._extract_levels_from_profile(self._session_levels)
        for level in current_levels:
            level_scores[level] = level_scores.get(level, 0.0) + 1.0
        
        # Prior session levels - lower weight but indicate persistence
        for session_levels_dict in self._prior_session_levels:
            prior_levels = self._extract_levels_from_profile(session_levels_dict)
            for level in prior_levels:
                level_scores[level] = level_scores.get(level, 0.0) + 0.5
        
        # Return sorted by persistence score (highest first)
        return sorted(level_scores.items(), key=lambda x: x[1], reverse=True)

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption at persistent volume levels.
        
        Prioritizes levels that appear across multiple sessions, indicating
        true structural support/resistance rather than noise.
        """
        if current.volume == 0:
            return None

        if current.tick_count < self.min_tick_count:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Require strict one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require clear wick showing price rejection
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

        # Check proximity to persistent levels
        level_scores = self._get_carried_levels_with_persistence()
        if not level_scores:
            return None

        # Find closest level and its persistence score
        closest_level = None
        min_distance = float('inf')
        closest_persistence = 0.0
        
        for level, persistence in level_scores:
            distance = abs(current.close - level)
            if distance < min_distance:
                min_distance = distance
                closest_level = level
                closest_persistence = persistence
        
        # Require proximity to a level
        if min_distance > self.level_proximity:
            return None
        
        # Prefer levels with higher persistence (appeared in multiple sessions)
        # But still allow current-session-only levels if very close
        min_required_persistence = 0.5 if min_distance > 0.25 else 0.0
        if closest_persistence < min_required_persistence:
            return None

        # Calculate stop and target based on bar structure
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            target_distance = stop_distance * self.target_multiple
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }
        
        else:  # Short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            target_distance = stop_distance * self.target_multiple
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry with absorption tag."""
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
