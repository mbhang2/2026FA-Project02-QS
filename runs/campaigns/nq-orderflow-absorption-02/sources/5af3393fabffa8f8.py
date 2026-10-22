"""
Volume profile absorption strategy - refined toward #81's -0.0096 performance.

Campaign analysis of best performers:
- #81: -0.0096 with 622 trades (BEST SCORE)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades

The previous trial with initiation filtering produced byte-identical synthetic
trades (70 trades), meaning the initiation logic didn't filter in the synthetic
environment. This suggests the initiation conditions were either:
1. Never met in synthetic data (too strict)
2. Not actually triggering differently from absorption-only

Key insight: The campaign asks to test initiation, but the test must produce
DIFFERENT trades to be meaningful. The byte-identical result means we need to
make the initiation logic actually filter in practice.

This trial takes a different approach:
1. Simplify initiation to be more achievable - it was too strict
2. Make absorption detection slightly less strict to generate more candidates
3. Keep the core multi-session level carrying that #81 uses
4. Focus on the parameters that made #81 successful: delta_ratio ~0.30, proximity ~1.0

The hypothesis: #81's -0.0096 came from:
- Moderate selectivity (622 trades, not 1079 or 2763)
- Proper level carrying from prior sessions
- Balanced absorption detection (not too strict, not too loose)
- Target multiple around 3.0

This trial adjusts:
- Slightly lower delta_ratio (0.30 vs 0.35) to generate more absorption candidates
- Simpler initiation check: just requires body > 0.25 and delta_ratio > 0.25
- Keep level_proximity at 1.0 (tight, proven)
- Restore target_multiple to 3.0 (matches #81)
- Keep stop distances tight (0.25-1.8)

Expected: Different trade distribution than previous trial, with ~600-800 trades
and score approaching #81's -0.0096.
"""

from __future__ import annotations

from typing import Optional
from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        level_proximity: float = 1.0,
        min_wick_points: float = 0.25,
        stop_buffer: float = 0.15,
        target_multiple: float = 3.0,
        max_stop_distance: float = 1.8,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_tick_count: int = 5,
        top_levels_per_session: int = 5,
        # Initiation parameters - simplified
        use_initiation_filter: bool = True,
        initiation_body_ratio: float = 0.25,
        initiation_delta_ratio: float = 0.25,
        initiation_lookback: int = 5,
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
        
        # Initiation parameters - simplified
        self.use_initiation_filter = use_initiation_filter
        self.initiation_body_ratio = initiation_body_ratio
        self.initiation_delta_ratio = initiation_delta_ratio
        self.initiation_lookback = initiation_lookback

        # Level tracking
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        
        # Track pending absorption setups waiting for initiation
        self._pending_setups: dict[int, dict] = {}  # session -> setup
        
        # Track recent bar ranges for initiation
        self._recent_ranges: deque = deque(maxlen=self.initiation_lookback)

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

        # Track recent ranges for initiation filter
        bar_range = current.high - current.low
        if bar_range > 0:
            self._recent_ranges.append(bar_range)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_setups.clear()
            return

        # Check if pending setup should be initiated
        if self.use_initiation_filter and self._pending_setups:
            setup = self._check_initiation(current)
            if setup:
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, setup, tag="absorption-confirmed")
                self._pending_setups.clear()
                return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            if self.use_initiation_filter:
                # Queue for initiation check on next bar
                self._pending_setups[current.session] = setup
            else:
                # Trade immediately
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, setup, tag="absorption")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and carry forward."""
        if self._session_levels:
            levels = self._extract_top_levels(self._session_levels)
            self._prior_session_levels.append(levels)
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        self._session_levels = {}
        self._entered_bar_timestamps = set()
        self._pending_setups.clear()

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

    def _extract_top_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract only the strongest levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        top_levels = [level for level, _ in sorted_items[:self.top_levels_per_session]]
        return sorted(top_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_top_levels(self._session_levels))
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """Detect high-quality absorption."""
        if current.volume == 0:
            return None

        if current.tick_count < self.min_tick_count:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        if delta > 0:
            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
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
        
        elif delta < 0:
            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
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

    def _check_initiation(self, current) -> Optional[dict]:
        """Check if current bar is a valid initiation after absorption."""
        if not self._pending_setups or current.session not in self._pending_setups:
            return None
        
        setup = self._pending_setups[current.session]
        
        if current.volume == 0:
            return None
        
        # Calculate average recent range
        if not self._recent_ranges:
            return None
        
        avg_range = sum(self._recent_ranges) / len(self._recent_ranges)
        bar_range = current.high - current.low
        
        if bar_range <= 0:
            return None
        
        # Check body ratio - simplified, just needs meaningful body
        body = abs(current.close - current.open)
        body_ratio = body / bar_range
        
        if body_ratio < self.initiation_body_ratio:
            return None
        
        # Check delta ratio - simplified, just needs directional flow
        delta_ratio = abs(current.delta) / current.volume if current.volume > 0 else 0
        if delta_ratio < self.initiation_delta_ratio:
            return None
        
        # Direction must match the setup
        if setup["direction"] == "long" and current.delta <= 0:
            return None
        if setup["direction"] == "short" and current.delta >= 0:
            return None
        
        return setup

    def _place_entry(self, ctx, setup: dict, tag: str) -> None:
        """Place entry order."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
