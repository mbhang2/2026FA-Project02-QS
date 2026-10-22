"""
Volume profile absorption strategy - simplifying initiation and improving selectivity.

Campaign analysis shows:
- #81: -0.0096 with 622 trades (BEST: 10x better than #76)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades

The synthetic test produced byte-identical 70 trades, confirming core logic works.

Key insight: The previous trial's initiation filtering with efficiency metrics
may be over-complicating the signal. #81's exceptional performance likely comes
from simpler, stricter absorption detection rather than complex initiation logic.

This trial removes the pending initiation mechanism and instead:
1. Tightens absorption detection parameters significantly
2. Removes the efficiency-based initiation confirmation (was too complex)
3. Simplifies to direct absorption-only entry with very strict filters
4. Uses delta_ratio of 0.42 (very strict one-sided flow)
5. Uses level_proximity of 1.0 (extremely tight to levels)
6. Requires min_wick_points of 0.75 (very pronounced rejection)
7. Keeps only top 2 levels per session (ultra-sparse)
8. Uses conservative target_multiple of 2.0 (tight risk/reward)

The hypothesis: #81's superior performance comes from extreme selectivity on
absorption detection itself, not from waiting for initiation. By removing the
complexity of initiation confirmation and instead making absorption detection
much stricter, we achieve better quality signals that trade less but win more.

This is a return to simpler, purer absorption detection with parameters that
match the spirit of #81's success: fewer trades of higher quality.

Expected: Score near #81's -0.0096 with 400-600 trades on real data.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.42,
        level_proximity: float = 1.0,
        min_wick_points: float = 0.75,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.0,
        max_stop_distance: float = 2.3,
        min_stop_distance: float = 0.25,
        size: int = 1,
        top_levels_per_session: int = 2,
        min_tick_count: int = 5,
        bars_into_session_before_trading: int = 1,
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
        self.top_levels_per_session = top_levels_per_session
        self.min_tick_count = min_tick_count
        self.bars_into_session_before_trading = bars_into_session_before_trading

        # Level tracking
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        self._bars_in_current_session: int = 0
        
        # Track entered bars to avoid duplicates
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
            self._bars_in_current_session = 0

        self._bars_in_current_session += 1

        # Accumulate volume profile
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Skip early session bars
        if self._bars_in_current_session <= self.bars_into_session_before_trading:
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
        """Extract the most significant levels."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        top_levels = [level for level, vol in sorted_items[:self.top_levels_per_session]]
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
        Detect very high-quality absorption at carried volume levels.
        
        Extremely strict parameters for maximum selectivity:
        - delta_ratio: 0.42 (very strong one-sided flow)
        - level_proximity: 1.0 (extremely tight to levels)
        - min_wick_points: 0.75 (very pronounced rejection)
        - target_multiple: 2.0 (tight risk/reward)
        - top_levels_per_session: 2 (only strongest nodes)
        """
        if current.volume == 0:
            return None

        # Require minimum tick activity
        if current.tick_count < self.min_tick_count:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Very strict one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require very pronounced wick showing strong price rejection
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

        # Check extremely tight proximity to carried level
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
