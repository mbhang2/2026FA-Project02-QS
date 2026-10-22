"""
Volume profile absorption strategy - test if synthetic identical trades are due to 
level proximity being too tight and missing real absorption signals.

Analysis of trial #60 (previous, produced identical synthetic trades):
- Level proximity 0.1 points (one tick) is TOO STRICT
- Baseline levels from training may not align with real session prices
- Missing absorption signals because price never hits baseline exactly
- Delta ratio 0.55 may also be too high (requiring extreme absorption only)

Key insight: If we're getting identical synthetic trades with stricter filters,
it means our filters are so tight they're not firing at all on real data either.
The synthetic baseline trades 70 times - if our absorption never fires, we'd get 0 trades.
Getting identical 70 trades means we're matching baseline behavior when we DO fire.

New hypothesis: We need to loosen the constraints to actually detect absorption:
1. **Level proximity 0.5 points** - allow reasonable proximity to baseline levels
2. **Delta ratio 0.40** - strong but not extreme absorption
3. **Wick ratio 0.20** - significant failed extension but not extreme
4. **Combine baseline + session levels** - use both carried and current session levels
5. **Keep strict stop/target bounds** - only enter setups with good risk/reward
6. **Accumulate session levels** - carry forward best levels from each session

The core problem with #60: level_proximity=0.1 is too tight. NQ moves in 0.25-point
bars and prices drift. Requiring price to be within 0.1 of a training level means
most absorption events are missed because the level has shifted slightly.

By relaxing to 0.5 points and combining baseline + session levels, we get:
- Absorption detection that actually fires on real data
- Still filtered by real volume levels (not just any bar)
- Different from synthetic baseline because our level detection differs

If this produces different trades on real data while synthetic stays at 70,
it means the looser filter is the key to finding real absorption.
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
        min_wick_ratio: float = 0.20,
        level_proximity: float = 0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 5.0,
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

        # Baseline levels from training (carried levels)
        self._baseline_levels: list[float] = []
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Current session volume profile
        self._session_profile: dict[float, int] = {}
        
        # Accumulated levels from prior sessions
        self._prior_session_levels: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Extract baseline levels from training split
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
        self._update_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels
        absorption = self._detect_absorption(current)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract and accumulate levels at session boundary."""
        if self._session_profile:
            session_levels = self._extract_levels_from_profile(self._session_profile)
            self._prior_session_levels.extend(session_levels)
            # Keep unique levels
            self._prior_session_levels = sorted(list(set(self._prior_session_levels)))
        
        self._entered_bar_timestamps = set()
        self._session_profile = {}

    def _extract_levels(self, bars) -> list[float]:
        """Extract significant volume levels from a sequence of bars."""
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

        # Get top 50% of volume levels
        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.50:
                break

        return sorted(list(set(top_levels)))

    def _update_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._session_profile[level_key] = self._session_profile.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _get_current_session_levels(self) -> list[float]:
        """Extract peak volume levels from current session."""
        if not self._session_profile:
            return []

        sorted_items = sorted(self._session_profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Get top 50% of volume in current session
        peak_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            peak_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.50:
                break

        return sorted(list(set(peak_levels)))

    def _get_all_carried_levels(self) -> list[float]:
        """Get all carried levels: baseline + prior sessions + current session."""
        all_levels = []
        all_levels.extend(self._baseline_levels)
        all_levels.extend(self._prior_session_levels)
        all_levels.extend(self._get_current_session_levels())
        return sorted(list(set(all_levels)))

    def _is_near_carried_level(self, price: float) -> bool:
        """Check if price is near any carried level."""
        carried = self._get_all_carried_levels()
        if not carried:
            return False
        
        min_distance = min(abs(price - level) for level in carried)
        return min_distance <= self.level_proximity

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong one-sided flow at a carried level with failed extension.
        
        Requirements:
        1. Price within 0.5 points of a carried level (reasonable proximity)
        2. Strong delta (>= 40% of volume one-sided)
        3. Significant wick opposite to flow (>= 20% of range)
        4. Stop distance within acceptable range
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        # Requirement 1: Near a carried level (baseline, prior, or current session)
        if not self._is_near_carried_level(current.close):
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Requirement 2: Strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Requirement 3: Significant wick opposite to flow
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
