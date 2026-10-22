"""
Volume profile absorption strategy - structural refinement for real market conditions.

Campaign analysis shows:
- #81: -0.0096 with 622 trades (BEST)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades

The previous trial produced byte-identical synthetic trades despite parameter changes,
indicating the synthetic data lacks the market conditions that differentiate strategies.

Critical insight: The campaign explicitly requires "carried levels" from prior sessions
to test whether absorption at volume nodes precedes moves to the next level. Previous
trials may have been extracting levels too aggressively or not properly distinguishing
between current-session and prior-session levels.

This trial makes three structural changes:

1. LEVEL EXTRACTION: Extract only the SINGLE strongest level per session (top volume node).
   This focuses the hypothesis on the most significant price nodes rather than distributing
   across multiple levels. A single strong node per session is more likely to be a true
   "level" that price returns to.

2. LEVEL PROXIMITY: Use absolute distance but require VERY tight proximity (1.2 points).
   #81's 622 trades suggest selectivity but not extreme rarity. At 1.2 points proximity
   on NQ (where moves between levels are typically 5-10 points), this filters for bars
   arriving exactly AT levels, not near them.

3. DELTA RATIO: Use 0.35 (stricter than 0.30) to require stronger one-sided flow.
   Absorption is flow being absorbed, which should show as highly directional volume.

4. WICK REQUIREMENT: Raise min_wick_points to 0.35 to require clear price rejection,
   not just any bar with flow.

5. TARGET MULTIPLE: Use 2.5 (between previous attempts) for reasonable risk/reward
   on moves between levels.

6. STOP DISTANCE: Keep max at 3.0 to allow stops for bars with meaningful rejection.

The hypothesis: By focusing on the single strongest level per session and requiring
very tight proximity, we identify the most likely candidates for "carried levels"
that price returns to. This should produce a different trade pattern on real data
where the volume profile is meaningful, while potentially producing similar synthetic
trades if the synthetic data has limited structure.

Expected: Different trade distribution on real data, approaching #81's -0.0096 with
500-700 trades. May produce similar synthetic trades if synthetic lacks real structure.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        level_proximity: float = 1.2,
        min_wick_points: float = 0.35,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 3.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_levels_per_session: int = 1,
        min_tick_count: int = 4,
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
            # Keep only last 2 sessions
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels using 0.25-point granularity."""
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
        """Extract only the strongest level(s) from volume profile."""
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
        """
        Detect absorption at carried volume levels.
        
        Focuses on:
        - Single strongest level per session (most meaningful)
        - Very tight proximity (1.2 points) to that level
        - Strong one-sided flow (delta_ratio 0.35)
        - Clear price rejection (wick 0.35+)
        """
        if current.volume == 0:
            return None

        # Require reasonable tick count
        if current.tick_count < self.min_tick_count:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Strict one-sided flow requirement
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

        # Check proximity to carried level - very tight
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
