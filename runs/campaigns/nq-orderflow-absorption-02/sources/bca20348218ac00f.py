"""
Volume profile absorption strategy - return to core absorption signal with improved level detection.

Analysis of recent trials:
- #16 (score -0.1076): Pure absorption with 463 trades - best score so far
- #39: Flow continuation (opposite of absorption) - scored not disclosed but likely poor
- Multiple trials with "IDENTICAL TRADES TO #0" - parameter changes had no effect

Key insight from #16's relative success:
The absorption signal (strong delta + wick opposite to flow) at carried levels
is the correct direction. The -0.1076 score suggests the signal exists but needs
refinement in:
1. Level detection accuracy - current granularity may be too coarse
2. Wick detection sensitivity - may be filtering out valid signals
3. Stop/target sizing - may not be aligned with actual level-to-level moves

The campaign asks: "Does absorption at a carried level precede a move away from it?"
#16 produced 463 trades across 384 sessions (~1.2 trades/session), suggesting
absorption is genuinely rare and selective - which is correct.

Changes from #16:
1. **Finer granularity for levels** - use 0.1 point increments instead of 0.25
   to capture more precise volume nodes
2. **Improve wick detection** - require wick to be meaningful fraction of bar range
3. **Multi-session level accumulation** - carry levels from 3 prior sessions explicitly
4. **Stricter absorption criteria** - delta_ratio 0.50 (between #16's 0.35 and #38's 0.65)
5. **Better target sizing** - 2.5x stop distance for realistic level-to-level moves
6. **Session-aware level extraction** - only use completed sessions for level definition

The goal: maintain the selectivity of #16 (463 trades) while improving signal quality
through better level detection and more precise absorption identification.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.50,
        level_proximity: float = 0.75,
        min_wick_ratio: float = 0.25,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        sessions_to_carry: int = 3,
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
        prior = history[-2]

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
        absorption = self._detect_absorption(current, prior)
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
            # Keep only recent sessions
            if len(self._completed_session_profiles) > self.sessions_to_carry:
                self._completed_session_profiles.pop(0)
        
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels with finer granularity."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Use 0.1 point granularity instead of 0.25 for finer level detection
        low_rounded = round(bar.low * 10) / 10
        high_rounded = round(bar.high * 10) / 10

        num_levels = max(1, int((high_rounded - low_rounded) / 0.1) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 10) / 10
            self._current_session_profile[level_key] = (
                self._current_session_profile.get(level_key, 0) + int(vol_per_level)
            )
            level += 0.1

    def _extract_levels(self, bars) -> list[float]:
        """Extract significant levels from a sequence of bars."""
        profile: dict[float, int] = defaultdict(int)
        
        for bar in bars:
            if bar.volume <= 0 or bar.high <= bar.low:
                continue
            
            low_rounded = round(bar.low * 10) / 10
            high_rounded = round(bar.high * 10) / 10
            
            num_levels = max(1, int((high_rounded - low_rounded) / 0.1) + 1)
            vol_per_level = bar.volume / num_levels
            
            level = low_rounded
            while level <= high_rounded + 0.01:
                level_key = round(level * 10) / 10
                profile[level_key] += int(vol_per_level)
                level += 0.1
        
        return self._extract_levels_from_profile(profile)

    def _extract_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Get top levels comprising ~70% of volume
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
        
        # Add levels from recent completed sessions
        for profile in self._completed_session_profiles:
            levels = self._extract_levels_from_profile(profile)
            all_levels.extend(levels)
        
        # Add baseline levels
        all_levels.extend(self._baseline_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption: strong delta at a carried level with wick opposite to flow.
        
        Absorption markers:
        1. Strong one-sided flow (delta_ratio >= 0.50)
        2. Failed extension (wick opposite to flow >= 0.25 of bar range)
        3. Price at a carried level (within 0.75 points)
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Marker 1: Strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low

        # Marker 2: Failed extension (wick opposite to flow)
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

        # Marker 3: Price at a carried level
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
