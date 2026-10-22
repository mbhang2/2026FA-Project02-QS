"""
Volume profile absorption strategy - targeting #81's -0.0096 with stricter delta filtering.

Campaign analysis: #81 achieved -0.0096 with 622 trades - the best score so far.
Byte-identical synthetic results indicate that flow quality checks and percentile-based
level extraction don't change behavior on synthetic noise.

The real insight from #81's success: it trades less frequently but with higher quality.
The synthetic test shows we need to change something that WILL trigger on synthetic data
to verify the logic is working.

Key change: Make the core delta_ratio threshold MUCH stricter (0.50 instead of 0.38).
This is a direct parameter change that will absolutely produce different trades on
synthetic data AND on real data. Combined with tighter wick requirements and proximity.

Refined approach targeting #81's quality:
1. Stricter delta_ratio (0.50) - only the cleanest one-sided flow
2. Tighter proximity (0.25 instead of 0.35) - must be RIGHT at the level
3. Higher min_wick_points (0.40) - stronger rejection signal
4. Higher min_wick_ratio (0.40) - wick must be substantial fraction of range
5. Higher min_body_ratio (0.35) - more directional bars only
6. Tighter max_stop_distance (0.80) - structural stops only
7. Lower target_multiple (1.8) - conservative, achievable targets
8. Require min_tick_count (6) - real compression

This directly changes the absorption detection threshold and should produce
different synthetic trades (not byte-identical). Expected: 300-450 trades with
score targeting -0.0050 or better, matching #81's quality improvement.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.50,
        level_proximity: float = 0.25,
        min_wick_points: float = 0.40,
        min_wick_ratio: float = 0.40,
        min_body_ratio: float = 0.35,
        stop_buffer: float = 0.08,
        target_multiple: float = 1.8,
        max_stop_distance: float = 0.80,
        min_stop_distance: float = 0.30,
        size: int = 1,
        min_tick_count: int = 6,
        volume_percentile: float = 0.75,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.min_wick_ratio = min_wick_ratio
        self.min_body_ratio = min_body_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_tick_count = min_tick_count
        self.volume_percentile = volume_percentile

        # Level tracking with full session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
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

    def _extract_levels_by_percentile(self, profile: dict[float, int]) -> list[float]:
        """
        Extract levels that represent the top volume_percentile of total volume.
        This adapts to session volatility and naturally selects the most significant levels.
        """
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Accumulate volume until we reach the percentile threshold
        cumulative_vol = 0
        threshold_vol = total_vol * self.volume_percentile
        selected_levels = []

        for level, vol in sorted_items:
            selected_levels.append(level)
            cumulative_vol += vol
            if cumulative_vol >= threshold_vol:
                break

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """
        Get all carried levels from current and prior sessions.
        """
        all_levels: dict[float, int] = {}
        
        # Current session levels
        for level in self._extract_levels_by_percentile(self._session_levels):
            all_levels[level] = all_levels.get(level, 0) + 1
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            for level in self._extract_levels_by_percentile(session_profile):
                all_levels[level] = all_levels.get(level, 0) + 1
        
        # Return unique levels sorted
        return sorted(list(all_levels.keys()))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption with very strict filtering.
        
        Key parameters targeting #81's quality:
        - delta_ratio >= 0.50 (very pure one-sided flow)
        - min_wick_points >= 0.40 (strong rejection)
        - min_wick_ratio >= 0.40 (wick is substantial)
        - min_body_ratio >= 0.35 (directional bar)
        - proximity <= 0.25 (right at level)
        - max_stop_distance <= 0.80 (tight structural stop)
        - target_multiple = 1.8 (conservative)
        """
        if current.volume == 0:
            return None

        # Require meaningful activity
        if current.tick_count < self.min_tick_count:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # VERY STRICT one-sided flow requirement - key change for different trades
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for meaningful body
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range
        if body_ratio < self.min_body_ratio:
            return None

        # Require clear wick showing price rejection (both absolute AND ratio)
        has_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0
            if lower_wick >= self.min_wick_points and wick_ratio >= self.min_wick_ratio:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0
            if upper_wick >= self.min_wick_points and wick_ratio >= self.min_wick_ratio:
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
