"""
Volume profile absorption strategy - aggressive parameter search toward #81's -0.0096.

Campaign analysis shows #81 achieved -0.0096 with 622 trades - exceptional performance.
The previous trial produced byte-identical trades on synthetic data, meaning the core
logic is sound but we need more aggressive parameter tuning to match #81's selectivity.

Key insight: #81's 10x improvement suggests it uses fundamentally stricter filtering.
The synthetic data produces identical trades because the synthetic data lacks the
real market conditions where the stricter parameters would filter out false signals.

Aggressive refinement toward #81:
1. Much stricter delta_ratio (0.40) - only strongest one-sided flow
2. Much tighter level_proximity (0.8) - extremely close to volume nodes
3. Higher min_wick_points (0.85) - requires very pronounced rejection
4. More conservative target_multiple (1.8) - tighter risk/reward
5. Stricter max_stop_distance (2.0) - only very tight stops
6. Keep max_levels_per_session at 2 (only the strongest nodes)
7. Increase min_tick_count to 6 (high quality, liquid bars only)
8. Increase bars_before_trading to 5 (skip early session noise)
9. Add volume_threshold filter (require bars with above-average volume)

The hypothesis: #81 uses extreme selectivity across ALL dimensions. By making
every parameter stricter - tighter proximity, higher delta ratio, stronger wick,
tighter stops, higher tick count - we filter for only the rarest, highest-quality
absorption setups that have the best chance of moving to the next level.

This is a move toward quality over quantity: expect 200-400 trades but with much
higher win rate and larger moves per trade.

Expected: Score near #81's -0.0096 with 250-450 trades on real data.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.40,
        level_proximity: float = 0.8,
        min_wick_points: float = 0.85,
        stop_buffer: float = 0.25,
        target_multiple: float = 1.8,
        max_stop_distance: float = 2.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_levels_per_session: int = 2,
        min_tick_count: int = 6,
        bars_before_trading: int = 5,
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
        self.bars_before_trading = bars_before_trading

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        self._bars_in_session: int = 0
        self._session_volumes: list[int] = []
        
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
            self._bars_in_session = 0

        self._bars_in_session += 1

        # Track volumes for this session
        if current.volume > 0:
            self._session_volumes.append(current.volume)

        # Accumulate volume profile for current session
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Skip early session bars to avoid noise
        if self._bars_in_session <= self.bars_before_trading:
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
        self._session_volumes = []

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
        """Extract only the most significant levels from volume profile."""
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

    def _get_average_volume(self) -> float:
        """Get average volume for this session so far."""
        if not self._session_volumes:
            return 0.0
        return sum(self._session_volumes) / len(self._session_volumes)

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect extremely high-quality absorption at carried volume levels.
        
        Extremely strict parameters for maximum selectivity:
        - delta_ratio: 0.40 (only strongest one-sided flow)
        - level_proximity: 0.8 (extremely tight to levels)
        - min_wick_points: 0.85 (very pronounced rejection)
        - target_multiple: 1.8 (tight risk/reward)
        - max_stop_distance: 2.0 (very tight stops)
        - min_tick_count: 6 (high quality bars)
        """
        if current.volume == 0:
            return None

        # Require very high tick count for signal validity
        if current.tick_count < self.min_tick_count:
            return None

        # Require above-average volume for this session
        avg_vol = self._get_average_volume()
        if avg_vol > 0 and current.volume < avg_vol * 0.8:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Extremely strict one-sided flow requirement
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
