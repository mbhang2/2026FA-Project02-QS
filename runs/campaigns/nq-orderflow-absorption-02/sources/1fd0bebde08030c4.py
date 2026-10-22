"""
Volume profile absorption strategy - testing initiation vs immediate entry.

Analysis of prior results:
- #16 scored -0.1076 (best: 463 trades)
- #13 scored -0.9744 (worst: 102,546 trades, pure momentum)
- Synthetic test shows IDENTICAL TRADES to baseline

Key insight: The synthetic identical trades indicates our absorption detection
is triggering on the SAME bars as momentum. The failed extension requirement
in #21 should have fixed this, but if synthetic still shows identical trades,
it means the absorption signal itself is not firing differently.

New approach: Rather than trying to make absorption "different" through
stricter conditions, test whether WAITING for initiation helps. The campaign
explicitly asks: "Does waiting for initiation help or just delay the entry?"

Strategy for this attempt:
1. **Simpler absorption detection**: Delta ratio 0.45, wick 0.05, at a level
2. **Two entry paths with different tags**:
   - "absorption-immediate": Enter on absorption bar itself
   - "absorption-initiated": Enter on next bar IF it shows initiation
3. **Initiation definition**: Bar with high efficiency (flow-adjusted) and 
   large body relative to recent range
4. **Track absorption bars**: Remember where absorption fired so we can detect
   initiation on the next bar
5. **Cost awareness**: Only trade if target is 2.5+ points away

This tests the initiation question directly: compare the two tag groups in
the run record. If initiation entries have better win rate or larger wins,
it helped. If they're the same or worse, waiting didn't help.

The reason this differs from baseline: we're not trading every close > open,
only bars with strong delta + wick + level proximity. The two entry types
let us measure whether waiting improves the outcome.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.45,
        min_wick_points: float = 0.05,
        level_proximity: float = 2.0,
        stop_buffer: float = 0.25,
        max_stop_distance: float = 3.5,
        min_stop_distance: float = 0.5,
        min_target_distance: float = 2.5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.min_target_distance = min_target_distance
        self.size = size

        # Level tracking
        self._baseline_levels: list[float] = []
        self._session_levels: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._all_prior_levels: list[float] = []
        
        # Track absorption bars for initiation detection
        self._last_absorption_bar: Optional[dict] = None
        self._last_absorption_close_ts: int = -1

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Build baseline levels from training split
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

        # Accumulate volume profile
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        prior = history[-2]

        # Check for initiation on current bar (from prior absorption)
        if self._last_absorption_bar and current.close_ts != self._last_absorption_close_ts:
            initiation = self._detect_initiation(current, prior, self._last_absorption_bar)
            if initiation:
                self._place_entry(ctx, initiation, tag="absorption-initiated")
                self._last_absorption_bar = None
                return

        # Detect new absorption signal
        absorption = self._detect_absorption(current, prior)
        if absorption:
            self._last_absorption_bar = absorption
            self._last_absorption_close_ts = current.close_ts
            # Place immediate entry
            self._place_entry(ctx, absorption, tag="absorption-immediate")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract levels from session and prepare for next."""
        if self._session_levels:
            levels = self._extract_levels_from_profile(self._session_levels)
            self._all_prior_levels.extend(levels)
        
        self._session_levels = {}
        self._last_absorption_bar = None
        self._last_absorption_close_ts = -1

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
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
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
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        all_levels.extend(self._all_prior_levels)
        all_levels.extend(self._baseline_levels)
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption: strong delta + wick + near level.
        
        Requirements:
        1. Delta ratio >= 0.45 (strong one-way flow)
        2. Wick >= 0.05 points (rejection of flow)
        3. Within 2.0 points of a carried level
        4. Valid stop distance (0.5-3.5 points)
        5. Target level exists and is at least 2.5 points away
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Requirement 1: Strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Requirement 2: Wick opposite to flow
        has_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Requirement 3: Price near a carried level
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
            
            # Requirement 4: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Requirement 5: Target level exists and is far enough
            above_levels = [l for l in carried_levels if l > current.close]
            if not above_levels:
                return None
            
            target_level = min(above_levels)
            target_distance = target_level - current.close
            
            if target_distance < self.min_target_distance:
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }
        
        else:  # Short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Requirement 4: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Requirement 5: Target level exists and is far enough
            below_levels = [l for l in carried_levels if l < current.close]
            if not below_levels:
                return None
            
            target_level = max(below_levels)
            target_distance = current.close - target_level
            
            if target_distance < self.min_target_distance:
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }

    def _detect_initiation(self, current, prior, absorption_bar: dict) -> Optional[dict]:
        """
        Detect initiation after absorption: efficient bar with large body.
        
        Initiation = bar that moves price efficiently relative to flow,
        with body large relative to recent range.
        
        Uses the stop/target from the original absorption bar.
        """
        if current.volume == 0:
            return None

        # Check if bar moves in the right direction
        direction = absorption_bar["direction"]
        if direction == "long":
            if current.close <= prior.close:
                return None  # Didn't move up
            body = current.close - current.open
            if body < 0.05:
                return None  # Body too small
        else:
            if current.close >= prior.close:
                return None  # Didn't move down
            body = current.open - current.close
            if body < 0.05:
                return None  # Body too small

        # Return the same setup but marked as initiated
        return {
            "direction": direction,
            "stop_distance": absorption_bar["stop_distance"],
            "target_distance": absorption_bar["target_distance"],
        }

    def _place_entry(self, ctx, setup: dict, tag: str) -> None:
        """Place entry with structural stop and target."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
