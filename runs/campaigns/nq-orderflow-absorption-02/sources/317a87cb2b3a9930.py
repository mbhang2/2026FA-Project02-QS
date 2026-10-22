"""
Volume profile absorption strategy - refined parameter tuning toward #81's -0.0096.

Campaign analysis of best performers:
- #81: -0.0096 with 622 trades (BEST SCORE - target to match)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades

The previous trial produced byte-identical synthetic trades (70 trades), meaning
the core logic is sound but parameters need adjustment. The byte-identical result
indicates the previous parameter changes didn't actually filter differently in the
synthetic environment.

Key observation: #81 achieved the best score with moderate trade frequency (622).
This suggests the edge comes from WHICH trades it takes, not the count. The
synthetic test shows the baseline logic works - we need to refine the filtering
to match #81's quality selection.

Analysis of what changed between #76 (1079 trades, -0.0893) and #81 (622 trades, -0.0096):
- Fewer trades (40% reduction) but 12x better score
- This is not about trading less; it's about higher-quality setups
- The filtering must be more selective about WHICH absorption patterns to trade

Hypothesis for #81's parameters:
1. delta_ratio: 0.35 (stricter than #76's implicit ~0.20, filters for pure one-sided flow)
2. level_proximity: 1.0 (tighter proximity to actual carried levels)
3. Fewer levels per session (5 instead of 7) - only the strongest matter
4. min_wick_points: 0.25 (requires clear price rejection evidence)
5. max_stop_distance: 1.8 (tighter stops = better risk/reward filtering)
6. target_multiple: 3.0 (lower than #76's 4.0, more realistic targets)
7. min_tick_count: 6 (requires meaningful activity)
8. stop_buffer: 0.15 (tighter structural stop placement)

This trial implements stricter filtering while maintaining the core logic that
produced the synthetic trades. The byte-identical synthetic result means we can
safely tighten filters without breaking the basic mechanism.

Expected: Trade count 550-700 with significantly better score than #76, targeting
#81's -0.0096 or better.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        level_proximity: float = 1.0,
        min_wick_points: float = 0.25,
        stop_buffer: float = 0.15,
        target_multiple: float = 3.0,
        max_stop_distance: float = 1.8,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_tick_count: int = 6,
        top_levels_per_session: int = 5,
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
        self.min_tick_count = min_tick_count
        self.top_levels_per_session = top_levels_per_session

        # Level tracking
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
            levels = self._extract_top_levels(self._session_levels)
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

        # Use 0.25-point granularity
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _extract_top_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract only the strongest levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        
        # Take only top N levels (fewer, stronger levels)
        top_levels = [level for level, _ in sorted_items[:self.top_levels_per_session]]
        
        return sorted(top_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        
        # Current session levels
        all_levels.extend(self._extract_top_levels(self._session_levels))
        
        # Prior session levels
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption with stricter filtering toward #81.
        
        Key differences from #76:
        - Stricter delta_ratio (0.35 vs ~0.20)
        - Tighter level_proximity (1.0 vs ~1.5)
        - Fewer levels per session (5 vs 7)
        - Higher min_wick_points (0.25 vs ~0.15)
        - Tighter max_stop_distance (1.8 vs 2.5)
        - Lower target_multiple (3.0 vs 3.5)
        """
        if current.volume == 0:
            return None

        # Require meaningful activity
        if current.tick_count < self.min_tick_count:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Strict one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require meaningful wick showing price rejection
        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

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

        # Check proximity to carried level - tighter
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Calculate stop and target with stricter constraints
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
