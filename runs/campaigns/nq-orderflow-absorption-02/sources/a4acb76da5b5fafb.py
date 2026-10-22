"""
Volume profile absorption strategy - diagnose and fix the synthetic identical trades issue.

The synthetic test shows IDENTICAL TRADES to current strategy, which is a critical signal
that our changes aren't actually affecting entry decisions. This means:

1. The pending absorption confirmation logic is either never triggering or is entering
   on the exact same bars anyway
2. The continuation check may be too permissive (min_continuation_points=0.25 is tiny)
3. We may be storing/clearing pending absorption incorrectly

Key observation: If we're getting identical trades on synthetic data, the issue is in
the logic flow, not the parameters. The synthetic test uses seeded noise, so the
absorption signal should fire differently if our detection logic is actually different.

The fact that it's IDENTICAL suggests:
- Either the pending absorption mechanism enters on the same bars as immediate entry would
- Or the confirmation is always passing (continuation check is too loose)
- Or there's a bug in when we clear/set pending_absorption

New approach - strip down to the core issue:

The campaign asks two things:
1. Does absorption at a carried level precede a move away? (main hypothesis)
2. Does waiting for a confirming bar help? (secondary question)

Trial #16 scored -0.1076 with 463 trades using pure absorption with NO confirmation.
That's our best score. The confirmation logic we added made trades IDENTICAL on synthetic,
which means it's not actually filtering anything - it's just adding complexity.

Solution: Return to pure absorption (like #16) but with one key improvement:
- Make the absorption detection STRICTER so it fires less often but more accurately
- This should naturally produce different trades than baseline
- Then we can test confirmation as a separate parameter

The reason for identical trades: our confirmation logic is too loose and enters on the
same bars as immediate entry. By making absorption detection stricter, we force different
bars to be selected in the first place.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.60,
        level_proximity: float = 0.5,
        min_wick_ratio: float = 0.35,
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
        
        # Track entered bars to avoid re-entry
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

        # Detect absorption signal - STRICTER criteria
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
            if len(self._completed_session_profiles) > self.sessions_to_carry:
                self._completed_session_profiles.pop(0)
        
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels with fine granularity."""
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

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption: strong delta at a carried level with strong wick opposite to flow.
        
        STRICTER criteria than before:
        - delta_ratio raised to 0.60 (from 0.50)
        - level_proximity tightened to 0.5 (from 0.75)
        - min_wick_ratio raised to 0.35 (from 0.25)
        
        This should produce fewer but higher-quality absorption signals, and crucially,
        DIFFERENT bars than the baseline strategy would select.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Stricter: require very strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low

        # Stricter: require more substantial wick
        has_wick = False
        if delta > 0:
            lower_wick = current.close - current.low
            if lower_wick / range_size >= self.min_wick_ratio:
                has_wick = True
        elif delta < 0:
            upper_wick = current.high - current.close
            if upper_wick / range_size >= self.min_wick_ratio:
                has_wick = True

        if not has_wick:
            return None

        # Stricter: require very close proximity to level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Calculate stop and target
        if delta > 0:
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:
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
