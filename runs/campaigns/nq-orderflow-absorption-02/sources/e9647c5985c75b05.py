"""
Volume profile absorption strategy - diagnose synthetic identical trades issue.

The synthetic test shows IDENTICAL trades to current strategy despite absorption
detection changes. This suggests one of:

1. The absorption detection never fires on synthetic data (too strict)
2. The synthetic baseline itself uses absorption logic
3. We're accidentally selecting bars via a different path that matches baseline

Key insight: Trial #16 scored -0.1076 with 463 trades. That's the only scored
result we have. All other attempts either scored worse or weren't scored.

The synthetic identical trades across multiple attempts suggests the problem is
FUNDAMENTAL to how we're detecting absorption, not a parameter tuning issue.

New hypothesis: The synthetic data may not have the volume/delta characteristics
that trigger absorption. The baseline strategy on synthetic data produces 70 trades.
If our absorption never fires there, we'd produce 0 trades and that would NOT be
identical - it would be different.

Since we're getting IDENTICAL trades, either:
- Our absorption IS firing and selecting the same bars as baseline
- OR we have a fallback that's matching baseline

Looking at the code: we have NO fallback. If absorption doesn't fire, we don't trade.
So identical trades means absorption IS firing and selecting the same bars.

This means our absorption detection is accidentally matching whatever the baseline
is doing. The baseline likely has momentum logic (close > open for longs, etc).

Solution: Make absorption detection MORE SELECTIVE, not less.

Changes:
1. Increase delta_ratio to 0.55 (stricter)
2. Require wick to be significant fraction of range (0.30)
3. Tighten level proximity to 0.5 points
4. Require bar to close in direction of delta (counter-trend wick)
5. Only carry levels from last 2 sessions (more relevant)

This should make absorption selective enough to produce DIFFERENT trades than
baseline while maintaining the -0.1076 quality signal.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.55,
        level_proximity: float = 0.5,
        min_wick_ratio: float = 0.30,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        sessions_to_carry: int = 2,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.sessions_to_carry = sessions_to_carry

        # Level tracking
        self._baseline_levels: list[float] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        self._completed_session_profiles: list[dict[float, int]] = []
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars:
                self._baseline_levels = self._extract_levels(train_bars)
        except Exception:
            pass

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            self._on_session_change()
            self._current_session = current.session

        # Accumulate volume profile for current session
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption signal
        absorption = self._detect_absorption(current)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract levels from completed session."""
        if self._current_session_profile:
            self._completed_session_profiles.append(self._current_session_profile)
            if len(self._completed_session_profiles) > self.sessions_to_carry:
                self._completed_session_profiles.pop(0)
        
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels with 0.25 point granularity."""
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

    def _extract_levels(self, bars) -> list[float]:
        """Extract significant levels from a sequence of bars."""
        profile: dict[float, int] = defaultdict(int)
        
        for bar in bars:
            if bar.volume <= 0 or bar.high <= bar.low:
                continue
            
            low_rounded = round(bar.low * 4) / 4
            high_rounded = round(bar.high * 4) / 4
            
            num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
            vol_per_level = bar.volume / num_levels
            
            level = low_rounded
            while level <= high_rounded + 0.01:
                level_key = round(level * 4) / 4
                profile[level_key] += int(vol_per_level)
                level += 0.25
        
        return self._extract_levels_from_profile(profile)

    def _extract_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.70:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get carried levels from recent completed sessions and baseline."""
        all_levels = []
        
        for profile in self._completed_session_profiles:
            levels = self._extract_levels_from_profile(profile)
            all_levels.extend(levels)
        
        all_levels.extend(self._baseline_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong delta at a carried level with counter-trend wick.
        
        Absorption = flow fails to extend:
        1. Strong one-sided delta (>= 0.55 of volume)
        2. Wick opposite to flow direction (>= 0.30 of bar range)
        3. Close against delta direction (counter-trend)
        4. Price at a carried level (within 0.5 points)
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Marker 1: Strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Marker 2 & 3: Wick opposite to flow + close against delta (counter-trend)
        has_absorption = False
        if delta > 0:  # Buying pressure
            # Need lower wick AND close near low (failed extension upward)
            lower_wick = current.close - current.low
            if lower_wick / range_size >= self.min_wick_ratio:
                # Also require close to be in lower half (against momentum)
                mid_point = (current.open + current.close) / 2
                if mid_point < (current.high + current.low) / 2:
                    has_absorption = True
        elif delta < 0:  # Selling pressure
            # Need upper wick AND close near high (failed extension downward)
            upper_wick = current.high - current.close
            if upper_wick / range_size >= self.min_wick_ratio:
                # Also require close to be in upper half (against momentum)
                mid_point = (current.open + current.close) / 2
                if mid_point > (current.high + current.low) / 2:
                    has_absorption = True

        if not has_absorption:
            return None

        # Marker 4: Price at a carried level
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
