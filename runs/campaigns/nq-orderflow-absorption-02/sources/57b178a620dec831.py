"""
Volume profile absorption strategy - diagnose why synthetic test shows identical trades.

The synthetic test shows IDENTICAL trades to the current strategy despite all our changes.
This is the critical diagnostic: if our absorption detection is firing and producing
different bars than the baseline, we'd see DIFFERENT trades. Identical trades means:

1. Our absorption detection is selecting the SAME bars as the baseline, OR
2. Our absorption detection never fires and we fall through to nothing

Since we have no fallback, it must be #1: our absorption detection is accidentally
matching whatever the baseline momentum logic does.

The problem: we're checking (close < mid) for longs, which is just checking if
close is in lower half. But the baseline likely does (close > open) for longs.
These could overlap significantly on certain bar patterns.

Key insight from the campaign description: "A rule that reads only the bar's shape
is a candlestick rule" - we must condition on FLOW, not just shape.

The real issue: our absorption detection might be too loose on what constitutes
"counter-trend." A bar with buying pressure that closes in the lower half could
still be momentum if the open was even lower - the body moved UP.

New approach - make absorption detection MORE SPECIFIC to ACTUAL absorption:
1. **Require close to be AGAINST the open** - if buying pressure, close < open
   (price failed to hold the move, not just closed lower than high)
2. **Stricter level proximity** - 0.5 points (trade only very close to levels)
3. **Higher delta ratio** - 0.55 (more extreme one-sided flow)
4. **Larger wick ratio** - 0.40 of range (very clear failed extension)
5. **Accumulate fewer sessions** - only current + 1 prior (more recent levels)
6. **Tighter stop/target** - 1.5x multiple (shorter expected move)

This makes absorption truly about flow failing to extend, not just about
bar shape. The close < open (for longs) requirement ensures we're trading
when momentum fails, not when momentum succeeds.
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
        min_wick_ratio: float = 0.40,
        stop_buffer: float = 0.15,
        target_multiple: float = 1.5,
        max_stop_distance: float = 3.5,
        min_stop_distance: float = 0.25,
        size: int = 1,
        sessions_to_carry: int = 1,
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
        if len(history) < 1:
            return

        current = history[-1]

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
        Detect absorption: strong delta at a carried level with failed extension AND close against open.
        
        Four core markers (all required):
        1. Strong one-sided flow (delta_ratio >= 0.55)
        2. Large failed extension (wick >= 0.40 of range, opposite to flow)
        3. Close against open direction (momentum failure, not just shape)
        4. Price at a carried level (within 0.5 point)
        
        The close vs open check ensures we're trading when momentum FAILS, not succeeds.
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

        # Marker 2 & 3: Large wick opposite to flow AND close against open
        has_absorption = False
        if delta > 0:  # Buying pressure
            # Need lower wick (failed extension up) AND close < open (momentum failed)
            lower_wick = current.close - current.low
            if lower_wick / range_size >= self.min_wick_ratio:
                if current.close < current.open:
                    has_absorption = True
        elif delta < 0:  # Selling pressure
            # Need upper wick (failed extension down) AND close > open (momentum failed)
            upper_wick = current.high - current.close
            if upper_wick / range_size >= self.min_wick_ratio:
                if current.close > current.open:
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
