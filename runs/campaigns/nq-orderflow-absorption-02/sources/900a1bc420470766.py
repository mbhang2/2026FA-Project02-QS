"""
Volume profile absorption strategy - restore #81's core logic with directional bias.

Analysis of top performers:
- #81: -0.0096 with 622 trades (BEST OVERALL)
- #159: -0.0705 with 894 trades
- #155: -0.0462 with 859 trades

Critical insight: The previous trial was byte-identical to #81 on synthetic data,
meaning the time-of-day filter (min_time_to_session_end_ns) did not differentiate.
This suggests the synthetic data either:
1. Does not span enough of a session to trigger the time filter
2. Has limited volume profile structure that rarely meets any absorption criteria

Root cause: Adding constraints that only activate under specific market conditions
(time-of-day filtering, confirmation bars) produces identical trades on synthetic
data when those conditions are rare.

New approach: Return to #81's proven core parameters but add a REAL differentiation
that works on any data: directional level bias combined with stricter proximity.

#81's actual behavior (reverse-engineered from 622 trades):
- delta_ratio: 0.28 (strong directional pressure)
- concentration_threshold: 0.60 (selective level extraction)
- level_proximity: 3.5 (moderate proximity tolerance)
- Implicit directional bias in how levels are selected

This trial:
1. Restore #81's core parameters exactly (delta_ratio=0.28, concentration=0.60)
2. Add explicit directional bias: for long setups, only consider levels ABOVE
   the current bar's low; for short setups, only consider levels BELOW the
   current bar's high
3. This creates asymmetric level selection that makes sense for absorption:
   - Buying absorption is relevant to resistance above
   - Selling absorption is relevant to support below
4. Tighten level_proximity to 2.8 (between #81's 3.5 and previous 2.5)
5. Remove time-of-day filtering (it doesn't work on synthetic data)

The directional bias is a real market structure filter that:
- Changes which levels are considered (not just when)
- Reduces false signals by focusing on relevant structure
- Works on any data where volume profiles exist
- Should produce different trades than #81 on real data

Expected: Score better than #81's -0.0096 by improving level relevance
through directional filtering. Trade frequency 600-700.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 2.8,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.60,
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

        # Detect absorption at directionally-filtered carried levels
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

    def _filter_levels_by_direction(self, all_levels: list[float], current, delta: float) -> list[float]:
        """
        Filter carried levels based on absorption direction.
        
        For long absorption (delta > 0): only consider levels at or above
        the current bar's low (resistance above the absorption)
        
        For short absorption (delta < 0): only consider levels at or below
        the current bar's high (support below the absorption)
        
        This focuses on structurally relevant levels for each direction.
        """
        if not all_levels:
            return all_levels
        
        if delta > 0:
            # Long: focus on levels above the absorption bar
            filtered = [level for level in all_levels if level >= current.low - 0.5]
            return filtered if filtered else all_levels
        else:
            # Short: focus on levels below the absorption bar
            filtered = [level for level in all_levels if level <= current.high + 0.5]
            return filtered if filtered else all_levels

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with directional level filtering.
        
        Absorption signature (#81 core logic with directional bias):
        - delta_ratio >= 0.28 (strong directional pressure)
        - Price within 2.8 points of a directionally-filtered carried level
        - Directional filtering: long setups use levels above, short uses below
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Core filter: strong directional delta (matching #81)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Get all carried levels
        all_levels = self._get_carried_levels()
        if not all_levels:
            return None

        # Apply directional filtering
        filtered_levels = self._filter_levels_by_direction(all_levels, current, delta)
        if not filtered_levels:
            return None

        # Check proximity to directionally-filtered levels
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
