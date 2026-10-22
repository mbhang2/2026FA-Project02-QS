"""
Volume profile absorption strategy - refined toward #81's -0.0096 breakthrough.

Campaign analysis reveals #81 achieved the best score (-0.0096) with 622 trades,
suggesting a "sweet spot" between strictness and capture rate.

Key observations:
- #81: -0.0096 with 622 trades (BEST - 9.8x better than #76)
- #76: -0.0893 with 1079 trades
- #87: NOT SCORED - likely too strict (delta_ratio=0.38)
- Previous trials with identical synthetic trades suggest parameter space is narrow

The synthetic test shows 70 trades consistently, which represents the noise floor
on that data. Real data will show the actual performance difference.

Critical insight: The current strategy produces identical synthetic trades to #76,
meaning the core logic is sound. The difference must come from parameter tuning
that only manifests on real data with actual volume structure.

#81's parameters (inferred from 622 trades and best score):
- delta_ratio: 0.30-0.34 (strong but not extreme - filters noise effectively)
- level_proximity: 3.0-3.5 (close to levels but allows natural variation)
- min_wick_points: 0.20-0.25 (clear rejection without over-filtering)
- target_multiple: 3.0-3.5 (targets moves between levels, not scalps)
- max_levels_per_session: 7-8 (balanced set, not too restrictive)

This trial moves toward #81's likely "optimal balance" with a key refinement:
the prior strategy produced identical synthetic trades because the parameters
didn't actually change behavior on that noise data. This trial makes a structural
change that WILL affect synthetic trades: it adds a second confirmation layer
that filters for sustained absorption rather than single-bar spikes.

Changes from previous:
1. Add absorption_confirmation: require the absorption bar's delta to be
   consistent with the prior bar's direction (multi-bar absorption pattern)
2. delta_ratio: 0.31 (balanced absorption detection)
3. level_proximity: 3.0 (tight to actual levels)
4. min_wick_points: 0.22 (clear signal)
5. target_multiple: 3.1 (conservative reward)
6. max_levels_per_session: 7 (balanced coverage)

The confirmation mechanism filters out single-bar noise by requiring the
absorption bar to align with the prior bar's flow direction, which will
reduce synthetic trades from 70 and should improve real data performance
toward #81's level.

Expected: Score approaching #81's -0.0096 with 500-650 trades on real data.
Synthetic trades should reduce to ~50-60 due to confirmation requirement.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.31,
        level_proximity: float = 3.0,
        min_wick_points: float = 0.22,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.1,
        max_stop_distance: float = 4.8,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_levels_per_session: int = 7,
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
        if len(history) < 2:  # Need prior bar for confirmation
            return

        current = history[-1]
        prior = history[-2]

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
        setup = self._detect_absorption(current, prior)
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
        """Extract the most significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)

        # Keep only top N most significant levels
        top_levels = [level for level, vol in sorted_items[:self.max_levels_per_session]]

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        
        # Current session levels
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        
        # Prior session levels
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption calibrated toward #81's optimal parameters.
        
        Key parameters targeting #81's -0.0096 performance:
        - delta_ratio: 0.31 (strong absorption, balanced)
        - level_proximity: 3.0 (meaningful proximity to levels)
        - min_wick_points: 0.22 (clear rejection signal)
        - target_multiple: 3.1 (targets moves between levels)
        
        Added confirmation: absorption must align with prior bar's flow
        direction to filter out single-bar noise.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Strong absorption threshold
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Confirmation: current absorption should align with prior bar's direction
        # This filters out isolated spikes and focuses on sustained flow patterns
        prior_delta = prior.delta
        if delta > 0 and prior_delta <= 0:
            return None  # Current is long but prior was short/neutral
        if delta < 0 and prior_delta >= 0:
            return None  # Current is short but prior was long/neutral

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

        # Check proximity to carried level
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
