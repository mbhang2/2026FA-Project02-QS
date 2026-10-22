"""
Volume profile absorption strategy - testing bar body ratio as quality filter.

Analysis of top performers:
- #81: -0.0096 with 622 trades (BEST OVERALL)
- #155: -0.0462 with 859 trades
- #151: -0.0576 with 992 trades
- #146: -0.0611 with 907 trades

Critical observation: The synthetic test shows byte-identical trades across
many parameter variations. This means the synthetic data lacks the specific
conditions needed to differentiate absorption quality. The real data must
contain patterns that synthetic noise cannot reproduce.

Key insight from #81's success: It achieved the best score with moderate
selectivity (622 trades). The question is: what makes these 622 trades special?

Hypothesis: #81's success may come from an implicit body-size requirement.
Absorption bars should have meaningful bodies relative to their range - a bar
that is all wick and no body is not absorption, it's just a thin spike.

This trial adds an explicit body requirement:
- bar_body_ratio: minimum (close - open) / (high - low) for the bar
- For long setups (delta > 0): body must be positive and >= 0.3 of range
- For short setups (delta < 0): body must be negative and >= 0.3 of range

This ensures:
1. The bar actually moved in the direction of absorption
2. The body is substantial relative to total range
3. We filter out thin-wicked bars that just happen to have directional delta

Combined with #81's proven parameters:
- delta_ratio: 0.28 (strict)
- level_proximity: 6.0 (moderate)
- min_wick_points: -0.5 (allows all wicks)
- bar_body_ratio: 0.30 (new - requires meaningful body)

This maintains the multi-session level carrying the campaign requires while
adding a body-based quality filter that should improve selectivity.

Expected: Score near #81's -0.0096, with 500-650 trades, by filtering for
absorption bars with meaningful bodies, not just wicks.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 6.0,
        min_wick_points: float = -0.5,
        bar_body_ratio: float = 0.30,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.50,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.bar_body_ratio = bar_body_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.concentration_threshold = concentration_threshold

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
        """Accumulate volume at price levels with 0.25-point granularity."""
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

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract levels where volume is above concentration threshold."""
        if not profile:
            return []

        max_vol = max(profile.values()) if profile else 0
        if max_vol == 0:
            return []

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

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with bar body ratio requirement.
        
        Absorption signature:
        - Strong directional delta (delta_ratio >= 0.28)
        - Meaningful body relative to range (body_ratio >= 0.30)
        - Close near a carried level (within 6.0 points)
        
        The body ratio filter ensures the bar actually moved in the direction
        of the delta, not just had a wick in that direction.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Core filter: strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check bar body ratio: meaningful body in direction of delta
        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        body = current.close - current.open
        body_ratio = abs(body) / bar_range if bar_range > 0 else 0

        # Body must be substantial and in the right direction
        if delta > 0:  # Long: need positive body (close > open)
            if body <= 0 or body_ratio < self.bar_body_ratio:
                return None
        elif delta < 0:  # Short: need negative body (close < open)
            if body >= 0 or body_ratio < self.bar_body_ratio:
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
