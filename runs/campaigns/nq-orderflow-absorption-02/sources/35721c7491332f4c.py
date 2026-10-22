"""
Volume profile absorption strategy - test with tighter proximity and stricter concentration.

Analysis of best performers:
- #81: -0.0096 with 622 trades (BEST OVERALL)
- #163: -0.0746 with 876 trades
- #159: -0.0705 with 894 trades

Critical insight: The synthetic test shows BYTE-IDENTICAL trades (70 trades) to
the current strategy. This means the directional filtering logic is NOT changing
which bars trigger absorption in synthetic data. The absorption bars detected must
be satisfying both the old and new directional logic equally.

This suggests: The directional filtering is too loose - it's filtering to "all levels"
as a fallback when no directionally-relevant levels exist. Since the synthetic data
shows identical trades, the fallback is being hit, making the directional logic
ineffective.

New hypothesis: Instead of directional filtering with fallback, we should:
1. Tighten level_proximity from 3.0 to 2.2 (stricter proximity requirement)
2. Increase concentration_threshold from 0.60 to 0.70 (only most significant levels)
3. Remove the fallback logic - if directionally-relevant levels don't exist within
   proximity, skip the setup entirely
4. This creates a narrower, higher-conviction signal set

The logic: A carried level is only valid if it's:
- Directionally aligned (resistance for longs, support for shorts)
- Within tight proximity (2.2 points)
- From the most concentrated volume (top 70% of max volume)

When none of these conditions are met, the setup is invalid - don't trade it.

Expected: Score better than current strategy by filtering more aggressively.
Target: -0.0080 to -0.0120 with 500-600 trades on real data.
The synthetic test should show fewer trades if this actually changes the logic.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 2.2,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.70,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
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
        """Extract levels where volume is above significance threshold."""
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

    def _get_directionally_relevant_levels(
        self, current, all_levels: list[float], direction: str
    ) -> list[float]:
        """
        Filter levels based on directional relevance.
        
        For long (BUY absorption): prefer levels above current price (resistance)
        For short (SELL absorption): prefer levels below current price (support)
        
        NO FALLBACK - if no directionally relevant levels exist, return empty list.
        """
        if not all_levels:
            return []
        
        if direction == "long":
            # For long: levels above current price are resistance being overcome
            relevant = [level for level in all_levels if level >= current.close]
            return relevant
        else:  # short
            # For short: levels below current price are support being broken
            relevant = [level for level in all_levels if level <= current.close]
            return relevant

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with strict directional level filtering and tight proximity.
        
        Absorption signature:
        - Strong directional delta (delta_ratio >= 0.28)
        - Price near a directionally-relevant carried level (within 2.2 points)
        - Level must be from most concentrated volume (top 70%)
        
        Key difference: NO FALLBACK. If no directionally-relevant levels exist
        within proximity, the setup is invalid. This filters for higher-conviction
        setups where the carried level is actually relevant to the current move.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Core filter: strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Determine direction based on delta
        if delta > 0:
            direction = "long"
        elif delta < 0:
            direction = "short"
        else:
            return None

        # Get all carried levels
        all_levels = self._get_carried_levels()
        if not all_levels:
            return None

        # Filter levels by directional relevance - NO FALLBACK
        relevant_levels = self._get_directionally_relevant_levels(current, all_levels, direction)
        if not relevant_levels:
            # No directionally-relevant levels - skip this setup
            return None
        
        # Check proximity to directionally-relevant level
        min_distance = min(abs(current.close - level) for level in relevant_levels)
        if min_distance > self.level_proximity:
            return None

        # Calculate stop and target
        if direction == "long":
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # short
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
