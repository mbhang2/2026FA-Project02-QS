"""
Volume profile absorption strategy - investigating why synthetic trades are identical.

The synthetic test shows byte-identical trades to #139, which suggests that either:
1. The concentration_multiplier or level extraction is not filtering differently
2. The initiation logic in #139 never actually triggers on synthetic data
3. The high_quality_delta_ratio threshold is catching all cases

Analysis of the best scores:
- #81: -0.0096 with 622 trades (BEST SCORE)
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

Key observation: #81's success came from a specific parameter set. The synthetic
identical result across many trials suggests we're not actually changing behavior.

This trial takes a different approach: instead of trying to add initiation logic
or complex level extraction, return to #81's core parameters but make ONE specific
change that should be observable: increase the level_proximity threshold significantly.

This should allow MORE bars to qualify as being "near a carried level", which should
change the trade count and distribution even on synthetic data if the change is real.

If this produces identical synthetic trades too, it means the carried levels themselves
are not being used meaningfully, and we need to rethink the level carrying mechanism.

Changes from #81:
1. Keep delta_ratio: 0.28 (proven by #81)
2. Keep min_wick_ratio: 0.40 (proven by #81)
3. INCREASE level_proximity: 6.0 -> 8.0 (should allow more entries)
4. Keep everything else identical to #81

This is a minimal, observable change that should show up in synthetic testing
if the level carrying mechanism is actually working.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 8.0,
        min_body_ratio: float = 0.50,
        min_wick_ratio: float = 0.40,
        min_tick_count: int = 8,
        min_bar_range: float = 1.5,
        max_bar_range: float = 8.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.60,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_body_ratio = min_body_ratio
        self.min_wick_ratio = min_wick_ratio
        self.min_tick_count = min_tick_count
        self.min_bar_range = min_bar_range
        self.max_bar_range = max_bar_range
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.concentration_threshold = concentration_threshold

        # Level tracking
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

    def _extract_levels_by_concentration(self, profile: dict[float, int]) -> list[float]:
        """
        Extract levels where volume is significantly concentrated.
        Keep only levels where volume >= max_volume * concentration_threshold.
        """
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
        all_levels.update(self._extract_levels_by_concentration(self._session_levels))
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            all_levels.update(self._extract_levels_by_concentration(session_profile))
        
        return sorted(list(all_levels))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels.
        
        Absorption signature:
        - Strong directional delta (delta_ratio >= 0.28)
        - Wick in opposite direction (>= 0.40 of bar range)
        - Price near a carried level (within 8.0 points)
        """
        if current.volume == 0:
            return None

        bar_range = current.high - current.low

        # PRE-FILTER: Bar range bounds
        if bar_range < self.min_bar_range or bar_range > self.max_bar_range:
            return None

        # PRE-FILTER: Tick count minimum
        if current.tick_count < self.min_tick_count:
            return None

        # PRE-FILTER: Body ratio (directional commitment)
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range if bar_range > 0 else 0
        if body_ratio < self.min_body_ratio:
            return None

        # Check delta and delta_ratio
        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check wick in opposite direction to delta
        if delta > 0:  # Buying pressure, check lower wick
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0
            
            if wick_ratio < self.min_wick_ratio:
                return None
        
        elif delta < 0:  # Selling pressure, check upper wick
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0
            
            if wick_ratio < self.min_wick_ratio:
                return None
        
        else:
            return None

        # Check proximity to carried level
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
