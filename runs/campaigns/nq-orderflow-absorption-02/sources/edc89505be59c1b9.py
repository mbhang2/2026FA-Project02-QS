"""
Volume profile absorption strategy - restoring level proximity with refined parameters.

The diagnostic trial showed that removing level proximity produced identical synthetic
trades, confirming that the absorption bars themselves (not level proximity) are the
binding constraint. This means:

1. The absorption detection filters are working correctly
2. The level carrying mechanism is being built but not needed to filter further
3. The real opportunity is in refining WHAT absorption bars we detect

Best scores so far:
- #81: -0.0096 with 622 trades (BEST)
- #76: -0.0893 with 1079 trades
- #86: -0.0600 with 673 trades

#81's success came from strict filtering. The diagnostic revealed that level proximity
doesn't add filtering on this data - the absorption signature itself is selective enough.

This trial restores level proximity (as required by campaign) but focuses on the real
optimization: tuning the absorption detection parameters to match #81's quality.

Key changes:
1. Restore level_proximity constraint (required by campaign, but not binding on this data)
2. Restore carried levels (required by campaign)
3. Tune absorption parameters to #81's proven values:
   - delta_ratio: 0.28 (strict)
   - min_wick_ratio: 0.40 (strict)
   - min_body_ratio: 0.50 (strict)
   - min_tick_count: 8 (strict)
4. Set level_proximity: 6.0 (wider than #81 to allow more entries, since proximity isn't binding)
5. Keep target_multiple: 4.0 (proven by #81)

The hypothesis: #81 scored well because of strict absorption detection, not because of
level proximity filtering. By keeping the strict filters and a reasonable level proximity,
we should achieve similar quality to #81 (score around -0.01 to -0.02, trades around 600-800).
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 6.0,
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
        level_volume_threshold: float = 0.30,
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
        self.level_volume_threshold = level_volume_threshold

        # Level tracking with session history
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

        # Detect absorption
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
            # Keep only last 2 sessions as required by campaign
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels with 0.25-point granularity."""
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
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """
        Extract levels where volume is significant.
        A level is significant if its volume >= max_volume * threshold.
        """
        if not profile:
            return []

        max_vol = max(profile.values()) if profile else 0
        if max_vol == 0:
            return []

        threshold_vol = max_vol * self.level_volume_threshold
        significant_levels = [level for level, vol in profile.items() if vol >= threshold_vol]

        return sorted(significant_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = set()
        
        # Current session levels
        all_levels.update(self._extract_significant_levels(self._session_levels))
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels using #81's proven strict parameters.
        
        Absorption signature requires:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick in opposite direction (>= 0.40 of bar range)
        3. Proper bar structure (body >= 0.50, range 1.5-8.0, ticks >= 8)
        4. Proximity to carried level (within 6.0 points)
        """
        if current.volume == 0:
            return None

        bar_range = current.high - current.low

        # PRE-FILTER 1: Bar range bounds (strict)
        if bar_range < self.min_bar_range or bar_range > self.max_bar_range:
            return None

        # PRE-FILTER 2: Minimum tick count (strict)
        if current.tick_count < self.min_tick_count:
            return None

        # PRE-FILTER 3: Body ratio (strict - directional commitment)
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range if bar_range > 0 else 0
        if body_ratio < self.min_body_ratio:
            return None

        # Check delta and delta_ratio (strict)
        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check wick in opposite direction to delta (absorption signature)
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

        # Check proximity to carried level (required by campaign)
        carried_levels = self._get_carried_levels()
        if carried_levels:
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
