"""
Volume profile absorption strategy - investigating directional bias and initiation.

Campaign analysis reveals an important pattern:
- #81: -0.0096 with 622 trades (BEST: 10x better than #76)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

Key insight: #81's exceptional performance (10x better than #76) with similar
trade frequency (622 vs 1079) suggests the difference is not just parameter
tuning but directional selectivity or entry timing.

The campaign explicitly asks about initiation: should we wait for a confirming
bar after absorption, or trade absorption alone? #81's superior performance
might come from directional bias - perhaps absorption at certain levels or with
certain flow characteristics predicts better than others.

This trial tests a hypothesis: directional asymmetry in absorption signals.
Some flow patterns at levels may be more predictive than others. Rather than
treating long and short absorption identically, we'll:

1. Apply stricter criteria to one direction (e.g., require higher delta_ratio
   for shorts than longs, or vice versa)
2. Use different target multiples per direction
3. Track which direction performs better in the data

Additionally, we'll be more selective about WHICH bars we process:
- Require minimum tick_count to avoid sparse/illiquid bars
- Require bars to be part of a session with sufficient prior activity
- Skip the first few bars of each session (warm-up period)

The hypothesis: #81 likely filters by direction preference or bar quality,
not just raw absorption metrics. By adding directional asymmetry and bar
quality filters, we can achieve similar selectivity with better performance.

Expected: Score near #81's -0.0096 with 500-700 trades on real data.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        level_proximity: float = 1.5,
        min_wick_points: float = 0.65,
        stop_buffer: float = 0.25,
        target_multiple_long: float = 2.3,
        target_multiple_short: float = 2.5,
        max_stop_distance: float = 2.8,
        min_stop_distance: float = 0.25,
        size: int = 1,
        top_levels_per_session: int = 2,
        min_bar_body: float = 0.25,
        min_tick_count: int = 5,
        bars_into_session_before_trading: int = 2,
        delta_ratio_long: float = 0.33,
        delta_ratio_short: float = 0.37,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple_long = target_multiple_long
        self.target_multiple_short = target_multiple_short
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.top_levels_per_session = top_levels_per_session
        self.min_bar_body = min_bar_body
        self.min_tick_count = min_tick_count
        self.bars_into_session_before_trading = bars_into_session_before_trading
        self.delta_ratio_long = delta_ratio_long
        self.delta_ratio_short = delta_ratio_short

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        self._bars_in_current_session: int = 0
        
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
            self._bars_in_current_session = 0

        self._bars_in_current_session += 1

        # Accumulate volume profile for current session
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Skip early session bars (warm-up)
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

        # Keep only top N most significant levels (very selective)
        top_levels = [level for level, vol in sorted_items[:self.top_levels_per_session]]

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

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption at carried volume levels.
        
        Now with directional asymmetry:
        - Longs: delta_ratio >= 0.33 (slightly more lenient)
        - Shorts: delta_ratio >= 0.37 (stricter)
        
        This tests whether one direction is more reliable than the other,
        which could explain #81's superior performance.
        """
        if current.volume == 0:
            return None

        # Require minimum tick activity to avoid sparse bars
        if current.tick_count < self.min_tick_count:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Require substantial bar body (not just wick)
        bar_range = current.high - current.low
        if bar_range <= 0:
            return None
        
        body = abs(current.close - current.open)
        if body / bar_range < self.min_bar_body:
            return None

        # Require pronounced wick showing price rejection
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

        # Directional delta ratio thresholds
        if delta > 0:  # Long
            if delta_ratio < self.delta_ratio_long:
                return None
        else:  # Short
            if delta_ratio < self.delta_ratio_short:
                return None

        # Check very tight proximity to carried level
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
                "target_distance": stop_distance * self.target_multiple_long,
            }
        
        else:  # Short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple_short,
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
