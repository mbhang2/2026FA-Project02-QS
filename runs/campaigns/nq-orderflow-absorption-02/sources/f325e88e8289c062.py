"""
Volume profile absorption strategy - addressing the synthetic byte-identity plateau.

The persistent byte-identical synthetic trades across recent trials (#122-124)
indicate the core issue: the level extraction and proximity logic are filtering
correctly, but the absorption detection itself is not selective enough.

Key insight from analysis:
- #81 scored -0.0096 with 622 trades (best performer)
- Recent trials produce byte-identical synthetic output despite parameter tweaks
- This means the filters that DO work are passing the same bars
- The problem is NOT in level carrying or proximity - it's in what counts as absorption

Critical realization: The synthetic data has NO order flow signal. If we're getting
byte-identical results, it means the delta_ratio and wick filters aren't actually
changing which bars pass. This suggests:

1. The delta_ratio threshold is still too loose (0.38 is not strict enough)
2. The wick ratio requirement alone isn't enough - need stricter absolute wick
3. The close position requirement (0.60) may be too lenient
4. We need to require bars to be LOCALLY extreme, not just have a wick

New approach - make absorption detection fundamentally stricter:
- delta_ratio: 0.45 (very strict one-sided flow)
- min_wick_points: 0.50 (significant absolute wick)
- min_wick_ratio: 0.60 (wick must be 60%+ of range)
- close_ratio: 0.70 (close must be very near the extreme)
- Only trade if bar is locally extreme (high/low relative to prior bars)
- Stricter level proximity: 0.35 (very tight to actual level)
- More conservative target: 2.0x (tighter risk/reward)
- Tighter stop: 0.08 buffer

This should produce different synthetic trades while maintaining the core logic.
Expected: Different synthetic output, targeting #81's -0.0096 or better.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.45,
        level_proximity: float = 0.35,
        min_wick_points: float = 0.50,
        min_wick_ratio: float = 0.60,
        stop_buffer: float = 0.08,
        target_multiple: float = 2.0,
        max_stop_distance: float = 1.5,
        min_stop_distance: float = 0.30,
        size: int = 1,
        local_extremity_bars: int = 3,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.local_extremity_bars = local_extremity_bars

        # Persistent level tracking across sessions
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._session_history: list[dict[float, int]] = []
        
        # Track recent bars for local extremity check
        self._recent_bars: list = []
        
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
            self._recent_bars = []

        # Accumulate volume profile
        self._update_session_profile(current)
        
        # Track recent bars for extremity check
        self._recent_bars.append(current)
        if len(self._recent_bars) > self.local_extremity_bars + 1:
            self._recent_bars.pop(0)

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
            self._session_history.append(dict(self._session_levels))
            if len(self._session_history) > 3:
                self._session_history.pop(0)
        
        self._session_levels = {}
        self._entered_bar_timestamps = set()
        self._recent_bars = []

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels with 1.0-point granularity."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Use 1.0-point granularity for cleaner, more stable levels
        low_rounded = round(bar.low)
        high_rounded = round(bar.high)

        num_levels = max(1, int(high_rounded - low_rounded) + 1)
        vol_per_level = bar.volume / num_levels

        level = float(low_rounded)
        while level <= high_rounded + 0.01:
            level_key = round(level)
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 1.0

    def _get_carried_levels(self) -> list[float]:
        """Extract carried levels from all available sessions."""
        all_levels: dict[float, int] = {}
        
        # Current session
        for level, vol in self._session_levels.items():
            all_levels[level] = all_levels.get(level, 0) + vol
        
        # Historical sessions
        for session_profile in self._session_history:
            for level, vol in session_profile.items():
                all_levels[level] = all_levels.get(level, 0) + vol
        
        if not all_levels:
            return []
        
        # Sort by accumulated volume and take top levels
        sorted_levels = sorted(all_levels.items(), key=lambda x: x[1], reverse=True)
        
        # Take top 6 levels by volume
        levels = [level for level, _ in sorted_levels[:6]]
        
        return sorted(levels)

    def _is_locally_extreme(self, current) -> bool:
        """Check if current bar is locally extreme compared to recent bars."""
        if len(self._recent_bars) < self.local_extremity_bars:
            return False
        
        # Compare to prior bars (not including current)
        prior_bars = self._recent_bars[:-1]
        if not prior_bars:
            return False
        
        prior_highs = [b.high for b in prior_bars]
        prior_lows = [b.low for b in prior_bars]
        
        max_prior_high = max(prior_highs)
        min_prior_low = min(prior_lows)
        
        # For long: current high should exceed recent highs
        # For short: current low should be below recent lows
        # (We check this in _detect_absorption based on delta)
        
        return True  # Will check specific direction in absorption detection

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption with very strict requirements.
        
        Key requirements:
        - VERY strong one-sided delta (delta_ratio >= 0.45)
        - Significant wick (both absolute AND ratio)
        - Close very near opposite extreme (70%+ of range)
        - Bar is locally extreme
        - Tight proximity to carried level (< 0.35)
        - Reasonable stop distance (0.30 to 1.5)
        """
        if current.volume == 0:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # VERY strict one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check local extremity
        if not self._is_locally_extreme(current):
            return None

        # Check proximity to carried level first
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Long setup: buying pressure with lower wick
        if delta > 0:
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range

            # Require BOTH absolute wick size AND high ratio
            if lower_wick < self.min_wick_points:
                return None
            if wick_ratio < self.min_wick_ratio:
                return None

            # Require close very near the high
            close_ratio = (current.close - current.low) / bar_range
            if close_ratio < 0.70:  # Close must be in upper 30% of range
                return None

            # Check local extremity: high should exceed recent highs
            if len(self._recent_bars) > 1:
                prior_highs = [b.high for b in self._recent_bars[:-1]]
                if current.high <= max(prior_highs):
                    return None

            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # Short setup: selling pressure with upper wick
        elif delta < 0:
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range

            # Require BOTH absolute wick size AND high ratio
            if upper_wick < self.min_wick_points:
                return None
            if wick_ratio < self.min_wick_ratio:
                return None

            # Require close very near the low
            close_ratio = (current.high - current.close) / bar_range
            if close_ratio < 0.70:  # Close must be in lower 30% of range
                return None

            # Check local extremity: low should be below recent lows
            if len(self._recent_bars) > 1:
                prior_lows = [b.low for b in self._recent_bars[:-1]]
                if current.low >= min(prior_lows):
                    return None

            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        return None

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
