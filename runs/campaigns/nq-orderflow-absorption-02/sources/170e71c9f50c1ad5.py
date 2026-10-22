"""
Volume profile absorption strategy - test aggressive decay with session-specific tracking.

Campaign analysis shows:
- #81: -0.0096 with 622 trades (baseline, immediate entry)
- #165: -0.0350 with 658 trades
- #155: -0.0462 with 859 trades
- #146: -0.0611 with 907 trades

The previous trial with level decay produced byte-identical trades, indicating the
decay mechanism wasn't filtering effectively. Analysis suggests the issue: decay
tracking persists across session boundaries, so levels from the previous session
still have low touch counts in the new session, making them appear "naked" when
they're actually from old data.

Key insight: Decay should be SESSION-SPECIFIC. A level touched in session N-1
should have maximum age/decay in session N, not carry forward its touch count.
This way:
1. Levels truly become "naked" only after price stops returning to them
2. Session boundaries naturally reset decay tracking
3. The "naked level" concept aligns with the campaign brief

This trial implements:
1. SESSION-SCOPED decay tracking: reset _level_last_touch on session boundary
   - Each session starts fresh with all levels at maximum decay weight
   - A level is only considered "touched" within the current session
   - This makes the naked level filter actually work as intended

2. AGGRESSIVE decay: min_decay_weight = 0.5
   - Level must not have been touched in last 50% of decay period
   - With level_decay_bars = 15, level must be untouched for 8+ bars

3. FASTER decay: level_decay_bars = 15 (unchanged from previous)
   - Levels become stale quickly within a session

4. TIGHTER level proximity: 3.5 points (unchanged)
   - Require close alignment to truly naked levels

5. REDUCED rolling window: 2 sessions (proven best)
   - Matches the best performers (#81, #165)

The synthetic test should show DIFFERENT trades because:
- Session-scoped decay resets tracking at boundaries
- This changes which levels qualify as "naked" at each bar
- The filter will now actually exclude recently-touched levels
- Entry decisions will change based on proper decay weighting

Expected: 400-550 trades with higher quality, targeting score -0.0120 to -0.0200.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 3.5,
        min_wick_points: float = -0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 2,
        concentration_threshold: float = 0.75,
        level_decay_bars: int = 15,
        min_decay_weight: float = 0.5,
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
        self.rolling_window_sessions = rolling_window_sessions
        self.concentration_threshold = concentration_threshold
        self.level_decay_bars = level_decay_bars
        self.min_decay_weight = min_decay_weight

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = -1
        self._current_session_profile: dict[float, int] = {}
        
        # Track when each level was last touched by price (session-scoped)
        self._level_last_touch: dict[float, int] = {}
        self._bars_in_session: int = 0
        
        # Track bars we've entered on to avoid re-entry
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

        # Increment bar counter within session
        self._bars_in_session += 1

        # Update which levels were touched by this bar
        self._update_touched_levels(current)

        # Accumulate volume into current session profile
        self._update_session_profile(current)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels and enter immediately
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Handle session boundary - add completed session to rolling window."""
        if self._current_session_profile:
            self._session_profiles.append(dict(self._current_session_profile))
            if len(self._session_profiles) > self.rolling_window_sessions:
                self._session_profiles.pop(0)
        
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()
        
        # RESET decay tracking for new session
        # Levels from previous session are now considered maximally aged
        self._level_last_touch = {}
        self._bars_in_session = 0

    def _update_touched_levels(self, bar) -> None:
        """Update which levels were touched by this bar's price action."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return
        
        # Round to 0.25-point granularity to match level accumulation
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4
        
        # Mark all levels the bar touched
        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._level_last_touch[level_key] = self._bars_in_session
            level += 0.25

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels for current session."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._current_session_profile[level_key] = (
                self._current_session_profile.get(level_key, 0) + int(vol_per_level)
            )
            level += 0.25

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract levels representing the top concentration_threshold of volume."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        cumulative_vol = 0
        selected_levels = []
        
        for level, vol in sorted_items:
            selected_levels.append(level)
            cumulative_vol += vol
            if cumulative_vol >= total_vol * self.concentration_threshold:
                break

        return sorted(selected_levels)

    def _get_level_decay_weight(self, level: float) -> float:
        """
        Calculate weight for a level based on how long ago it was touched.
        
        Returns 1.0 if never touched in current session (truly naked).
        Returns < 1.0 if touched recently (decaying).
        Returns 0 if touched within last level_decay_bars bars.
        """
        if level not in self._level_last_touch:
            return 1.0  # Truly naked level (never touched in this session)
        
        bars_since_touch = self._bars_in_session - self._level_last_touch[level]
        
        # Exponential decay: full weight after level_decay_bars bars
        if bars_since_touch >= self.level_decay_bars:
            return 1.0
        
        # Linear decay from 0 to 1 over level_decay_bars
        return bars_since_touch / self.level_decay_bars

    def _get_carried_levels(self) -> list[tuple[float, float]]:
        """
        Get all carried levels with their decay weights.
        
        Returns list of (level, weight) tuples sorted by level.
        """
        level_weights: dict[float, float] = {}
        
        # Add levels from current session
        for level in self._extract_significant_levels(self._current_session_profile):
            weight = self._get_level_decay_weight(level)
            level_weights[level] = max(level_weights.get(level, 0), weight)
        
        # Add levels from prior sessions (all at max weight since we reset tracking)
        for session_profile in self._session_profiles:
            for level in self._extract_significant_levels(session_profile):
                # Levels from prior sessions are considered maximally aged
                level_weights[level] = max(level_weights.get(level, 0), 1.0)
        
        # Return sorted by level, with weights
        return sorted([(level, weight) for level, weight in level_weights.items()])

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels.
        
        Prioritizes truly naked levels (high decay weight).
        Uses stricter decay threshold to filter more aggressively.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Check proximity to carried level with decay weighting
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        # Find closest level and its weight
        min_distance = float('inf')
        best_weight = 0.0
        
        for level, weight in carried_levels:
            distance = abs(current.close - level)
            if distance < min_distance:
                min_distance = distance
                best_weight = weight
        
        if min_distance > self.level_proximity:
            return None
        
        # STRICTER decay filter: require level to be truly naked
        # A weight < 0.5 means the level was touched within the last 50% of decay period
        if best_weight < self.min_decay_weight:
            return None

        # LONG: Buying pressure with lower wick
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
        
        # SHORT: Selling pressure with upper wick
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
