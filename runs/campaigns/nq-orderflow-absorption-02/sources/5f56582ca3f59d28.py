"""
Volume profile absorption strategy - diagnose and fix the synthetic identical trades issue.

The synthetic test shows IDENTICAL trades to the current strategy, which means our
changes are not reaching the order placement. This happens when:

1. The absorption detection never fires (too restrictive)
2. The initiation logic prevents entry (pending_setup never converts to order)
3. Both absorption AND initiation are disabled by parameters

Key insight: The synthetic test produces 70 trades with identical fills. This means
the current strategy is trading SOMETHING consistently. Our absorption detection
with initiation is either:
- Never detecting absorption (too strict filters)
- Detecting absorption but initiation check always fails
- Both firing but parameters prevent order placement

Let's simplify radically to understand what's happening:

1. **Remove initiation complexity** - it's a state machine that may have bugs
2. **Use absorption ONLY** - the core campaign signal
3. **Relax all thresholds significantly** to ensure we detect SOMETHING different
4. **Keep level carryover** - the multi-session requirement
5. **Focus on delta_ratio and wick** - the two core absorption markers

If this still produces identical trades, it means:
- The synthetic baseline is also absorption-based (unlikely)
- OR the parameters are still filtering out all absorption signals
- OR there's a fundamental issue with how we're detecting bars

Changes from previous:
1. **Remove initiation entirely** - use_initiation=False to test pure absorption
2. **Very relaxed delta_ratio** - 0.30 (was 0.42)
3. **Generous level proximity** - 2.0 points (was 1.25)
4. **Minimal wick requirement** - 0.10 ratio (was 0.15)
5. **Simpler stop/target** - 2.0x multiple (was 2.5x)
6. **No efficiency tracking** - remove deque complexity
7. **Direct order placement** - no pending_setup state machine

If this produces different trades than the current strategy on synthetic data,
we've found the issue. If it still produces identical trades, the absorption
signal itself isn't firing and we need to debug the detection logic further.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        level_proximity: float = 2.0,
        min_wick_ratio: float = 0.10,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.0,
        max_stop_distance: float = 5.0,
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

        # Accumulate volume profile
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption signal - ONLY signal
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
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

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
        Detect absorption: strong delta at a carried level with wick opposite to flow.
        
        Simplified version without initiation complexity.
        Three markers:
        1. Strong one-sided flow (delta_ratio >= threshold)
        2. Failed extension (wick opposite to flow)
        3. Price at a carried level
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
