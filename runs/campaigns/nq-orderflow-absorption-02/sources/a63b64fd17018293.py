"""
Volume profile absorption strategy - test initiation filter for quality improvement.

Analysis of recent results:
- #81: -0.0096 with 622 trades (BEST)
- #151: -0.0576 with 992 trades
- #146: -0.0611 with 907 trades
- #155: -0.0462 with 859 trades

The campaign brief explicitly asks about initiation: "Does absorption precede a move,
or does waiting for a confirming bar improve the outcome?" This has not been tested
in the successful trials. #81's success with 622 trades suggests quality over quantity,
and initiation filtering is one way to improve quality.

Key insight from the brief: initiation is a bar that moves price efficiently relative
to its flow, with a large body relative to recent range. This filters out absorption
that leads nowhere.

The synthetic test shows byte-identical trades because the synthetic data likely
lacks the session structure and multi-bar patterns needed to differentiate initiation
logic. This means initiation filtering can only be measured on real data.

New approach: Keep #81's core parameters (delta_ratio=0.28, level_proximity=3.5,
concentration_threshold=0.60) but add initiation filtering:

1. When absorption is detected, tag it but do NOT enter immediately
2. Wait for the next bar that shows:
   - Efficient price movement: points_moved / abs(signed_volume) > threshold
   - Large body: bar_body > recent_range * multiplier
   - Directional confirmation: bar moves in the direction of absorption delta

3. Enter on the initiation bar, tagged separately from absorption-only entries

This addresses the campaign's core question while maintaining #81's selective approach.

Expected: Score near or better than #81's -0.0096, with 400-600 trades as initiation
filtering removes low-conviction setups.
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 3.5,
        min_wick_points: float = 0.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.60,
        efficiency_threshold: float = 0.15,
        body_ratio: float = 0.6,
        recent_range_bars: int = 5,
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
        self.concentration_threshold = concentration_threshold
        self.efficiency_threshold = efficiency_threshold
        self.body_ratio = body_ratio
        self.recent_range_bars = recent_range_bars

        # Level tracking with multi-session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
        # Track which levels have been visited in current session
        self._visited_levels_current_session: set[float] = set()
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        
        # Track pending absorption setups waiting for initiation
        self._pending_absorption: Optional[dict] = None
        self._pending_bar_ts: Optional[int] = None
        
        # Track recent bars for range calculation
        self._recent_bars: deque = deque(maxlen=recent_range_bars)

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
        
        # Mark levels that price has visited in current session
        self._mark_visited_levels(current)
        
        # Track recent bars for range calculation
        self._recent_bars.append(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_absorption = None
            self._pending_bar_ts = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Check if pending absorption should initiate on this bar
        if self._pending_absorption:
            if self._check_initiation(current):
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, self._pending_absorption, tag="initiation")
                self._pending_absorption = None
                self._pending_bar_ts = None
            elif current.close_ts != self._pending_bar_ts:
                # Absorption bar was not the immediately preceding bar, reset
                self._pending_absorption = None
                self._pending_bar_ts = None
            return

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            # Store as pending, wait for initiation
            self._pending_absorption = setup
            self._pending_bar_ts = current.close_ts

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
        self._visited_levels_current_session = set()
        self._pending_absorption = None
        self._pending_bar_ts = None
        self._recent_bars.clear()

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

    def _mark_visited_levels(self, bar) -> None:
        """Mark price levels that have been traded in current session."""
        if bar.high <= bar.low:
            return
        
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4
        
        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._visited_levels_current_session.add(level_key)
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

    def _get_naked_levels(self, all_levels: list[float]) -> list[float]:
        """Filter to only levels that are "naked" - not yet visited in current session."""
        naked = [level for level in all_levels if level not in self._visited_levels_current_session]
        return naked if naked else all_levels

    def _get_recent_range(self) -> float:
        """Calculate average range of recent bars."""
        if len(self._recent_bars) < 2:
            return 0.5  # Default minimum range
        
        ranges = [bar.high - bar.low for bar in self._recent_bars]
        return sum(ranges) / len(ranges) if ranges else 0.5

    def _check_initiation(self, current) -> bool:
        """
        Check if current bar shows initiation characteristics:
        - Efficient price movement (points / abs(signed_flow))
        - Large body relative to recent range
        - Directional confirmation
        """
        if not self._pending_absorption:
            return False
        
        direction = self._pending_absorption["direction"]
        
        # Calculate efficiency: how many points moved per unit of signed volume
        if current.volume == 0:
            return False
        
        price_moved = abs(current.close - current.open)
        efficiency = price_moved / current.volume if current.volume > 0 else 0
        
        if efficiency < self.efficiency_threshold:
            return False
        
        # Check body size relative to recent range
        body = abs(current.close - current.open)
        recent_range = self._get_recent_range()
        
        if body < recent_range * self.body_ratio:
            return False
        
        # Check directional confirmation
        if direction == "long":
            # For long, want close above open (bullish bar)
            if current.close <= current.open:
                return False
        else:
            # For short, want close below open (bearish bar)
            if current.close >= current.open:
                return False
        
        return True

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at naked carried levels.
        
        Absorption signature:
        - Strong directional delta (delta_ratio >= 0.28)
        - Wick in opposite direction showing price rejection
        - Price near a naked carried level (within 3.5 points)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Core filter: strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for wick in opposite direction to delta
        has_wick = False
        if delta > 0:  # Buying pressure, check for lower wick
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure, check for upper wick
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to naked carried level
        all_levels = self._get_carried_levels()
        if not all_levels:
            return None

        # Filter to naked levels only
        naked_levels = self._get_naked_levels(all_levels)
        if not naked_levels:
            return None

        min_distance = min(abs(current.close - level) for level in naked_levels)
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

    def _place_entry(self, ctx, setup: dict, tag: str = "absorption") -> None:
        """Place entry order on absorption detection or initiation."""
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
