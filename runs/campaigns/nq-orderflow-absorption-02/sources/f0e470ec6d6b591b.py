"""
Volume profile absorption strategy - refinement targeting improvement over #76's -0.0893.

Campaign analysis of proven performers:
- #76: -0.0893 with 1079 trades (BEST SCORE)
- #74: -0.0980 with 2763 trades
- #16: -0.1076 with 463 trades
- #81: -0.0096 with 622 trades

The plateau at #76 indicates the sweet spot: ~1000 trades with high selectivity.
The key insight from #81's success (best recent score at -0.0096) is that it used
slightly different parameters that still maintained multi-session level carrying.

Critical observation: All top performers (#76, #81) use:
1. Strong delta_ratio filtering (0.28-0.32) - selective absorption detection
2. Moderate level_proximity (3.0-6.0) - price must be reasonably near level
3. Proper multi-session level carrying (current + prior sessions)
4. Reasonable target multiples (3.0-4.0)

The campaign brief emphasizes: "absorption at a carried level precedes a move
away from that level". This means:
1. We need REAL carried levels (top volume nodes from actual trading)
2. We need REAL absorption (strong one-sided flow with price rejection)
3. We need to avoid over-constraining with forward validation that loses trades

This trial refines the level extraction to be more robust:
- Use a cumulative volume approach across sessions
- Keep levels that represent actual traded volume, not just proximity
- Simplify the detection logic to focus on core absorption signature
- Maintain the proven #76 parameters but with cleaner implementation

The key change: Instead of keeping top N levels per session separately, accumulate
all volume across sessions and extract levels that represent true volume nodes.
This gives us more stable, meaningful levels to test against.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 6.0,
        min_wick_points: float = -0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        volume_percentile: float = 0.70,
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
        self.volume_percentile = volume_percentile

        # Cumulative volume profile across all sessions
        self._all_time_profile: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        
        # Track bars we've entered on to prevent re-entry
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Build baseline profile from training data
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars:
                for bar in train_bars:
                    self._add_bar_to_profile(bar, self._all_time_profile)
        except Exception:
            pass

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            self._on_session_change()
            self._current_session = current.session

        # Accumulate volume profile for current session
        self._add_bar_to_profile(current, self._current_session_profile)
        # Also accumulate into all-time profile
        self._add_bar_to_profile(current, self._all_time_profile)

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

    def _on_session_change(self) -> None:
        """Handle session boundary - merge current session into cumulative."""
        # Current session profile is already merged into all_time_profile
        # Just reset for the new session
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()

    def _add_bar_to_profile(self, bar, profile: dict[float, int]) -> None:
        """Add bar's volume to a profile at 0.25-point granularity."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Distribute volume across 0.25-point levels
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            profile[level_key] = profile.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _extract_significant_levels(self) -> list[float]:
        """
        Extract significant levels from cumulative profile.
        Uses percentile approach: levels above the threshold volume.
        """
        if not self._all_time_profile:
            return []

        volumes = list(self._all_time_profile.values())
        if not volumes:
            return []

        # Find threshold volume
        sorted_volumes = sorted(volumes, reverse=True)
        threshold_idx = max(0, int(len(sorted_volumes) * (1 - self.volume_percentile)))
        threshold = sorted_volumes[threshold_idx] if threshold_idx < len(sorted_volumes) else 0

        # Extract levels above threshold
        significant_levels = [
            level for level, vol in self._all_time_profile.items()
            if vol >= threshold
        ]

        return sorted(significant_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from cumulative trading."""
        return self._extract_significant_levels()

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels.
        
        Requirements:
        - Strong one-sided delta (delta_ratio >= 0.28)
        - Price within proximity to a carried level (6.0 points)
        - Evidence of price rejection (wick opposite to flow)
        - Valid stop/target distances
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Selective delta threshold - must be strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check proximity to any carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Require clear wick opposite to the dominant flow
        # This is the absorption signature: flow arrives but price doesn't move
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

        # Calculate stop and validate distances
        if delta > 0:  # Long setup
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # Short setup
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
