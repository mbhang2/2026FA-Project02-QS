"""
Volume profile absorption strategy - refined for realistic signal detection and multi-session holding.

Analysis of prior results:
- #13 scored -0.9744 (best so far)
- Synthetic test shows identical trades to baseline momentum
- The core issue: absorption conditions are firing on the SAME bars as momentum (close > open)

Key insight from campaign question: We need to hold for moves BETWEEN levels, not scalp.
The cost model (0.2 points round-trip) means only multi-point moves survive. A 2.5x target
on a 0.75 point stop = 1.875 points gross, minus 0.2 points cost = 1.675 net. That's thin.

Strategy refinement:
1. **Stricter absorption detection**: Require both tail AND failed extension (not just either)
2. **Level-based targeting**: Target the next level in the profile, not a fixed multiple
3. **Realistic entry filtering**: Only enter if target level exists AND is far enough away
4. **Better level extraction**: Use all prior sessions, not just recent lookback
5. **Session-aware profile**: Build profile incrementally, extract at session boundaries
6. **Avoid double-entry**: Use close_ts for uniqueness

The synthetic test identical trades likely means our absorption fires on same bars as momentum.
By requiring BOTH tail AND failed extension (stricter), we filter to genuinely different bars.
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
        level_proximity: float = 1.0,
        min_wick: float = 0.05,
        stop_buffer: float = 0.25,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.5,
        min_target_distance: float = 2.0,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick = min_wick
        self.stop_buffer = stop_buffer
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.min_target_distance = min_target_distance
        self.size = size

        # Baseline levels from training
        self._baseline_levels: list[float] = []

        # Session tracking
        self._current_session: Optional[int] = None
        self._session_profile: dict[float, int] = {}
        
        # All extracted session levels (keep full history)
        self._all_session_levels: dict[int, list[float]] = {}
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Extract baseline levels from training split
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars and len(train_bars) > 0:
                self._baseline_levels = self._extract_levels_from_bars(train_bars)
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
            self._on_session_change(current.session)
            self._current_session = current.session

        # Accumulate bars into current session profile
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check for absorption on this bar
        prior = history[-2]
        if current.close_ts not in self._entered_bar_timestamps:
            absorption = self._detect_absorption(current, prior)
            if absorption:
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and prepare for new one."""
        if self._session_profile and self._current_session is not None:
            levels = self._extract_levels(self._session_profile)
            if levels:
                self._all_session_levels[self._current_session] = levels

        self._session_profile = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels in current session."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Distribute volume across 0.25 granularity
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_rounded = round(level * 4) / 4
            self._session_profile[level_rounded] = (
                self._session_profile.get(level_rounded, 0) + int(vol_per_level)
            )
            level += 0.25

    def _extract_levels_from_bars(self, bars) -> list[float]:
        """Extract significant levels from bars."""
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
                level_rounded = round(level * 4) / 4
                profile[level_rounded] += int(vol_per_level)
                level += 0.25
        
        return self._extract_levels(profile)

    def _extract_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract top levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Get top levels comprising ~70% of volume
        top_levels = []
        cumulative = 0
        for level, vol in sorted_items[:50]:
            top_levels.append(level)
            cumulative += vol
            if cumulative > total_vol * 0.70:
                break

        return sorted(top_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from all sources."""
        all_levels = list(self._baseline_levels)
        
        # Add levels from all prior sessions
        for session_levels in self._all_session_levels.values():
            all_levels.extend(session_levels)
        
        # Add current session levels if we have them
        if self._session_profile:
            current_levels = self._extract_levels(self._session_profile)
            all_levels.extend(current_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption: strong delta + visible tail + failed extension + near level.
        
        Requirements (ALL must be met):
        1. Delta ratio >= 0.50 (strong one-way flow)
        2. Visible wick opposite to flow direction
        3. Failed extension (close back past prior bar's extreme)
        4. Price within 1.0 points of a carried level
        5. Target level exists and is at least 2.0 points away
        
        This makes absorption different from simple momentum (close > open).
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Requirement 1: Strong one-way flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Requirement 2: Visible wick opposite to flow
        has_tail = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick > self.min_wick:
                has_tail = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick > self.min_wick:
                has_tail = True

        if not has_tail:
            return None

        # Requirement 3: Failed extension
        if delta > 0:  # Long absorption
            if current.close >= prior.high:
                return None  # Extended, not absorption
        else:  # Short absorption
            if current.close <= prior.low:
                return None  # Extended, not absorption

        # Requirement 4: Price near a carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        closest_level = None
        min_distance = float('inf')
        for level in carried_levels:
            distance = abs(current.close - level)
            if distance < min_distance:
                closest_level = level
                min_distance = distance

        if min_distance > self.level_proximity:
            return None

        # Requirement 5: Target level exists and is far enough
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Find target level above
            above_levels = [l for l in carried_levels if l > current.close]
            if not above_levels:
                return None
            
            target_level = min(above_levels)
            target_distance = target_level - current.close
            
            # Target must be meaningful (at least 2.0 points)
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
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Find target level below
            below_levels = [l for l in carried_levels if l < current.close]
            if not below_levels:
                return None
            
            target_level = max(below_levels)
            target_distance = current.close - target_level
            
            # Target must be meaningful (at least 2.0 points)
            if target_distance < self.min_target_distance:
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry with structural stop and target."""
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
