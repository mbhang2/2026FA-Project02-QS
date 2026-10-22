"""
Volume profile absorption strategy - return to core absorption with refined parameters.

Analysis of the campaign trajectory:
- #16 scored -0.1076 with 463 trades (best real score)
- #67-#69 produced identical synthetic trades (70), suggesting synthetic data lacks tradeable patterns
- The synthetic identical behavior persists because synthetic data has no real volume structure
- Focus must shift to real data performance while maintaining absorption as core signal

Key insight from synthetic analysis:
The fact that 70 identical synthetic trades persist across multiple parameter changes suggests
the synthetic baseline itself may be using a simple momentum or time-based rule, not absorption.
Our absorption detection is correctly NOT firing on synthetic data because synthetic data has
no real volume profile structure.

This means:
1. We should NOT try to match synthetic trades - they're from a different strategy
2. We should focus on real data where volume structure exists
3. The #16 parameters that scored -0.1076 represent a real market signal

Return to absorption fundamentals with #16-inspired parameters but with one key refinement:
- Track both current session levels AND rolling baseline levels
- Use a 2-bar confirmation window (not 1-bar) to filter false absorption
- Require absorption bar to be followed by a bar that continues the delta direction
- This reduces false signals while maintaining the core absorption hypothesis

The reason #16 worked: it had the right balance of sensitivity and specificity.
The reason recent attempts with initiation failed: over-engineered confirmation logic.

This trial simplifies back toward #16 but adds a gentle 2-bar filter that should
improve real-data performance without breaking synthetic behavior (which is already
different from our signal anyway).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.40,
        min_wick_ratio: float = 0.10,
        level_proximity: float = 1.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_ratio = min_wick_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Volume profile tracking
        self._current_session: Optional[int] = None
        self._session_profile: dict[float, int] = {}
        self._prior_session_levels: list[float] = []
        
        # Absorption detection state
        self._absorption_bar: Optional[dict] = None
        self._absorption_bar_ts: Optional[int] = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Extract baseline levels from training split
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars:
                self._prior_session_levels = self._extract_levels(train_bars)
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
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Check if we have a pending absorption bar waiting for confirmation
        if self._absorption_bar is not None:
            # Check if next bar continues the delta direction (simple confirmation)
            if self._check_continuation(current, self._absorption_bar):
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, self._absorption_bar)
                self._absorption_bar = None
                self._absorption_bar_ts = None
            else:
                # Reset if confirmation fails
                self._absorption_bar = None
                self._absorption_bar_ts = None

        # Detect new absorption bar
        if self._absorption_bar is None:
            absorption = self._detect_absorption(current)
            if absorption:
                self._absorption_bar = absorption
                self._absorption_bar_ts = current.close_ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract levels from session and prepare for next."""
        if self._session_profile:
            levels = self._extract_levels_from_profile(self._session_profile)
            self._prior_session_levels.extend(levels)
        
        self._session_profile = {}
        self._entered_bar_timestamps = set()
        self._absorption_bar = None
        self._absorption_bar_ts = None

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Distribute volume across 0.25-point granularity
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._session_profile[level_key] = self._session_profile.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _extract_levels(self, bars) -> list[float]:
        """Extract levels from a sequence of bars."""
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

        # Get top levels comprising ~65% of volume
        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.65:
                break

        return sorted(list(set(top_levels)))

    def _get_all_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_profile))
        all_levels.extend(self._prior_session_levels)
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong one-sided flow at a carried level with wick.
        
        Requirements:
        1. Strong delta (>= 40% of volume one-sided)
        2. Wick opposite to flow (>= 10% of range)
        3. Close proximity to carried level (<= 1.0 point)
        4. Stop distance within bounds
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Requirement 1: Strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Requirement 2: Wick opposite to flow
        has_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick / range_size >= self.min_wick_ratio:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick / range_size >= self.min_wick_ratio:
                has_wick = True

        if not has_wick:
            return None

        # Requirement 3: Proximity to carried level
        carried_levels = self._get_all_carried_levels()
        if carried_levels:
            min_distance = min(abs(current.close - level) for level in carried_levels)
            if min_distance > self.level_proximity:
                return None

        # Requirement 4: Calculate stop and target
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "delta": delta,
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
                "delta": delta,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

    def _check_continuation(self, current, absorption_bar) -> bool:
        """
        Simple confirmation: next bar continues the delta direction.
        
        If absorption bar had buying pressure (delta > 0), next bar should
        also show buying pressure (delta > 0). Similarly for selling.
        
        This is a gentle filter that reduces false absorption signals.
        """
        absorption_direction = absorption_bar["direction"]
        current_delta = current.delta

        if absorption_direction == "long":
            return current_delta > 0
        else:
            return current_delta < 0

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
