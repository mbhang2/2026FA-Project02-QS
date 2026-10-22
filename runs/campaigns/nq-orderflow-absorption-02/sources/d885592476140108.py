"""
Volume profile absorption strategy - addressing byte-identical synthetic test issue.

The synthetic test produced byte-identical trades, meaning the current parameters
are not actually filtering differently than the baseline. Analysis shows:

- #81: -0.0096 with 622 trades (BEST - 10x better than #76)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades

The problem: The current strategy's filters (momentum check, volume multiple, etc.)
are either not triggering in synthetic data OR they're redundant with the baseline.

Key insight from #81's success: It achieves 10x better score with similar trade count.
This suggests the edge is NOT about trade frequency but about WHICH trades it takes.

The momentum check requiring 2/3 bars same direction is likely the culprit for
byte-identical synthetic results - it's filtering out all signals because synthetic
data doesn't have the right flow patterns.

This trial removes the momentum check entirely and instead focuses on:
1. Tighter delta_ratio threshold (0.40 instead of 0.38) - stricter one-sided flow
2. Tighter level_proximity (0.50 instead of 0.75) - closer to actual levels
3. Stricter wick requirement (0.40 instead of 0.35) - more price rejection evidence
4. Tighter body_ratio (0.35 instead of 0.30) - stronger conviction bars
5. Remove volume_multiple filter - it's not differentiating in synthetic data
6. Tighter stop/target distances to reduce risk per trade

The hypothesis: #81's edge comes from stricter absorption definition (higher delta ratio,
tighter proximity, clearer wick) rather than adding momentum confirmation that filters
everything out. By making the core absorption filters tighter, we get fewer but higher
quality trades that should outperform.

Expected: Different trade distribution that actually triggers in synthetic data,
aiming for ~600-700 trades with better quality filtering.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.40,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.40,
        min_body_ratio: float = 0.35,
        stop_buffer: float = 0.25,
        target_multiple: float = 1.8,
        max_stop_distance: float = 2.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_tick_count: int = 4,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.min_body_ratio = min_body_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
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
        """Extract the most significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        
        # Keep top 8 levels
        levels = [item[0] for item in sorted_items[:8]]
        
        return sorted(list(set(levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption at carried volume levels.
        
        Uses tighter thresholds on core absorption metrics:
        - delta_ratio: 0.40 (stricter one-sided flow)
        - level_proximity: 0.50 (closer to actual levels)
        - wick: 0.40 points (clear price rejection)
        - body_ratio: 0.35 (stronger conviction)
        """
        if current.volume == 0:
            return None

        # Require decent tick count
        if current.tick_count < self.min_tick_count:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Require strong bar body
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range
        if body_ratio < self.min_body_ratio:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Stricter delta threshold - only highest quality one-sided flow
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

        # Check proximity to carried level with tight threshold
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
