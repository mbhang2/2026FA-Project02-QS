"""
Volume profile absorption strategy - test directional level bias with tighter filtering.

Analysis of top performers:
- #81: -0.0096 with 622 trades (BEST OVERALL)
- #155: -0.0462 with 859 trades
- #151: -0.0576 with 992 trades

The synthetic test shows byte-identical trades to the current strategy, meaning
the parameter changes (0.25 granularity, 0.65 threshold, 0.10 wick) do not
differentiate the synthetic data.

Key insight: The gap between #81 (622 trades) and #155 (859 trades) suggests
that directional bias in level selection matters. #155 explicitly biased levels
toward the direction of the move, but the current strategy does not.

Critical observation: The current strategy's min_wick_points=0.10 is very loose.
#81 used min_wick_points=-0.5, which means ANY wick >= -0.5 (essentially any bar
with a wick at all). But that's not the real constraint - the real constraint
in #81 was the combination of:
1. Tight level_proximity (3.5 vs current 3.0)
2. Strict concentration_threshold (0.60 vs current 0.65)
3. Directional level bias (not present in current)

The synthetic test shows 70 trades with byte-identical fills. This means the
synthetic data triggers the same conditions. To make a real change, I need to:
1. Restore directional bias in level selection (like #155 did)
2. Tighten concentration_threshold back to 0.60 (like #81)
3. Adjust level_proximity to 3.5 (sweet spot between #81 and #155)
4. Keep 0.25 granularity but ensure it's properly applied

The directional bias should filter carried levels to only those that make sense
for the current absorption direction, reducing false signals while maintaining
the finer granularity advantage.

Expected: Score near #81's -0.0096 with 650-800 trades by combining #81's
tight filtering with #155's directional bias and 0.25-point granularity.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 3.5,
        min_wick_points: float = 0.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.60,
        level_granularity: float = 0.25,
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
        self.concentration_threshold = concentration_threshold
        self.level_granularity = level_granularity

        # Level tracking with multi-session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
        # Track bars we've entered on to avoid re-entry
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
            self._prior_session_levels.append(dict(self._session_levels))
            # Keep only last 2 sessions
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels with configurable granularity."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Round to granularity (0.25 points - NQ tick size)
        low_rounded = round(bar.low / self.level_granularity) * self.level_granularity
        high_rounded = round(bar.high / self.level_granularity) * self.level_granularity

        num_levels = max(1, int(round((high_rounded - low_rounded) / self.level_granularity)) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level / self.level_granularity) * self.level_granularity
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += self.level_granularity

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract significant levels from volume profile using concentration threshold."""
        if not profile:
            return []

        max_vol = max(profile.values()) if profile else 0
        if max_vol == 0:
            return []

        # Use concentration threshold (60th percentile - strict filtering)
        threshold_vol = max_vol * self.concentration_threshold
        selected_levels = [level for level, vol in profile.items() if vol >= threshold_vol]

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = set()
        
        # Current session levels
        all_levels.update(self._extract_significant_levels(self._session_levels))
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _get_directionally_filtered_levels(self, current, all_levels: list[float]) -> list[float]:
        """
        Filter carried levels based on directional bias.
        
        For long setups (delta > 0): prefer levels at or above current price
        For short setups (delta < 0): prefer levels at or below current price
        
        This focuses on levels in the direction of the move, which makes sense
        for absorption: if buying pressure is absorbed, the relevant resistance
        is above; if selling pressure is absorbed, the relevant support is below.
        """
        if not all_levels:
            return all_levels
        
        delta = current.delta
        
        if delta > 0:
            # Long bias: prefer levels at or above current price
            biased = [level for level in all_levels if level >= current.close - 1.0]
            return biased if biased else all_levels
        elif delta < 0:
            # Short bias: prefer levels at or below current price
            biased = [level for level in all_levels if level <= current.close + 1.0]
            return biased if biased else all_levels
        
        return all_levels

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with directional level bias and strict filtering.
        
        Absorption signature:
        - Strong directional delta (delta_ratio >= 0.28)
        - Wick in opposite direction showing price rejection
        - Price near a directionally-filtered carried level (within 3.5 points)
        
        Combines #81's strict concentration threshold (0.60) with #155's
        directional bias and 0.25-point granularity for market structure accuracy.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Core filter: strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for wick in opposite direction to delta (absorption signature)
        has_wick = False
        if delta > 0:  # Buying pressure, check for lower wick (price rejection)
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure, check for upper wick (price rejection)
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to directionally-filtered carried level
        all_levels = self._get_carried_levels()
        if not all_levels:
            return None

        # Apply directional filter to reduce false signals
        filtered_levels = self._get_directionally_filtered_levels(current, all_levels)
        if not filtered_levels:
            return None

        min_distance = min(abs(current.close - level) for level in filtered_levels)
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
