"""
Volume profile absorption strategy - targeting #81's -0.0096 with refined level detection.

The byte-identical synthetic result indicates the structural change to track (volume, bar_count)
is not actually producing different behavior. The issue: the fallback path in _extract_true_nodes
still extracts levels that don't meet the concentration threshold, making it equivalent to the
previous approach.

This trial makes the filtering ACTUALLY DIFFERENT by:

1. REMOVING the fallback path entirely - if no levels meet concentration threshold, return empty
2. INCREASING volume_concentration_percentile from 0.20 to 0.35 - stricter threshold
3. INCREASING min_bar_touches from 2 to 3 - require more structural evidence
4. DECREASING max_levels_to_extract from 4 to 3 - even fewer, stronger levels
5. TIGHTENING absorption_delta_ratio to 0.42 - matching #81's quality bar
6. INCREASING level_proximity requirement to 0.50 - must be RIGHT at the level
7. RAISING min_wick_points to 0.35 - clearer price rejection signal
8. RAISING min_body_ratio to 0.30 - more directional bars only

These changes will produce materially different level extraction on real data because:
- The 0.35 concentration threshold is STRICT - most levels won't qualify
- Requiring 3+ bar touches is STRUCTURAL - filters out noise
- No fallback means we only trade at PROVEN nodes, not "good enough" levels
- Tighter proximity (0.50) means absorption must happen AT the level, not near it

Expected: 300-500 trades (fewer than #81's 622), with score better than -0.0600,
targeting #81's -0.0096 through higher quality setups.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.42,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.35,
        min_body_ratio: float = 0.30,
        stop_buffer: float = 0.10,
        target_multiple: float = 2.0,
        max_stop_distance: float = 1.5,
        min_stop_distance: float = 0.30,
        size: int = 1,
        min_tick_count: int = 6,
        volume_concentration_percentile: float = 0.35,
        min_bar_touches: int = 3,
        max_levels_to_extract: int = 3,
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
        self.volume_concentration_percentile = volume_concentration_percentile
        self.min_bar_touches = min_bar_touches
        self.max_levels_to_extract = max_levels_to_extract

        # Level tracking: store (level -> (total_volume, bar_count))
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, tuple[int, int]] = {}
        self._prior_session_levels: list[dict[float, tuple[int, int]]] = []
        
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
        """Accumulate volume at price levels, tracking bar touches."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            if level_key not in self._session_levels:
                self._session_levels[level_key] = (0, 0)
            
            total_vol, bar_count = self._session_levels[level_key]
            self._session_levels[level_key] = (total_vol + int(vol_per_level), bar_count + 1)
            level += 0.25

    def _extract_true_nodes(self, profile: dict[float, tuple[int, int]]) -> list[float]:
        """
        Extract only TRUE volume nodes - levels with structural significance.
        
        A level is a true node if:
        1. It was touched by multiple bars (bar_count >= min_bar_touches)
        2. It has volume >= percentile threshold of session max
        
        NO FALLBACK - if no levels meet criteria, return empty list.
        This ensures we only trade at proven, concentrated nodes.
        """
        if not profile:
            return []

        # Find maximum volume in session for percentile calculation
        max_volume = max((vol for vol, _ in profile.values()), default=0)
        if max_volume == 0:
            return []

        volume_threshold = max_volume * self.volume_concentration_percentile

        # Filter for true nodes: multiple touches AND meaningful volume concentration
        true_nodes = []
        for level, (total_vol, bar_count) in profile.items():
            # Must meet BOTH criteria: structural touches AND volume concentration
            if bar_count >= self.min_bar_touches and total_vol >= volume_threshold:
                true_nodes.append((level, total_vol, bar_count))

        # If no true nodes meet the strict criteria, return empty - don't trade
        if not true_nodes:
            return []

        # Sort by total volume and take top N
        true_nodes.sort(key=lambda x: x[1], reverse=True)
        extracted = [level for level, _, _ in true_nodes[:self.max_levels_to_extract]]

        return sorted(extracted)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        
        # Current session
        all_levels.extend(self._extract_true_nodes(self._session_levels))
        
        # Prior sessions
        for session_profile in self._prior_session_levels:
            all_levels.extend(self._extract_true_nodes(session_profile))
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption at true volume nodes.
        
        Requirements:
        - Strong one-sided delta (delta_ratio >= 0.42)
        - Meaningful activity (tick_count >= 6)
        - Clear price rejection (wick >= 0.35)
        - Meaningful body (body_ratio >= 0.30)
        - RIGHT at true node (< 0.50 points)
        - Reasonable stop distance (0.30 to 1.5)
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

        # Strict one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for meaningful body
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range
        if body_ratio < self.min_body_ratio:
            return None

        # Require clear wick showing price rejection
        has_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to carried true node
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
