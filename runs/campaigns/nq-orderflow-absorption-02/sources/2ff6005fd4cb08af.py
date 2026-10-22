"""
Volume profile absorption strategy - testing if synthetic identical trades issue is fundamental.

The synthetic test shows IDENTICAL TRADES to the baseline on 70 trades. This is the key
diagnostic: it means our absorption detection is selecting the exact same bars as whatever
the baseline strategy does, even on seeded noise.

Critical insight from the campaign brief:
- The question asks for moves BETWEEN levels, not trades at levels
- Multi-session holding is expected (moves between levels span sessions)
- The floor is 50% of sessions because single-session trades won't survive costs
- Previous order-flow attempts failed at high frequency (7+ round turns per session)

The synthetic identical trades reveal the real problem: we're trading too frequently
and matching the baseline's frequency pattern. The baseline likely trades momentum
multiple times per session. Our absorption detection, even with "failed extension"
filters, still fires on bars that match the baseline's selection.

New approach - focus on RARE, STRUCTURAL absorption:
1. Only trade when absorption occurs AT MAJOR LEVELS (top 30% by volume)
2. Require absorption to be EXTREME (delta_ratio 0.70+, not 0.50)
3. Only when there's a MULTI-SESSION target (levels 2+ sessions away)
4. Much stricter wick requirement (0.25+ points, not 0.15)
5. Accumulate levels across FULL HISTORY, not just recent sessions
6. Only enter if target level exists 2+ sessions into the future

This should produce FEWER trades (different from baseline's frequency), focus on
structural levels (genuinely different signal), and hold longer (matching campaign's
expectation of multi-session moves).

The synthetic test will show if this produces different trades. If still identical,
it means the problem is deeper - either the baseline IS absorption-based, or our
level extraction fundamentally matches theirs.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.70,
        level_proximity: float = 0.50,
        min_wick_points: float = 0.25,
        stop_buffer: float = 0.25,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.5,
        min_target_points: float = 3.0,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.min_target_points = min_target_points
        self.size = size

        # Full level history across all sessions
        self._all_levels: list[float] = []
        self._current_session: Optional[int] = None
        self._session_profile: dict[float, int] = {}
        self._recent_bars: list = []
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Build baseline levels from training split
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars:
                self._all_levels = self._extract_major_levels(train_bars)
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
        
        # Keep recent bars for context
        self._recent_bars.append(current)
        if len(self._recent_bars) > 100:
            self._recent_bars.pop(0)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect EXTREME absorption at MAJOR levels only
        absorption = self._detect_extreme_absorption(current, prior)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract major levels from completed session."""
        if self._session_profile:
            major_levels = self._extract_major_levels_from_profile(self._session_profile)
            self._all_levels.extend(major_levels)
            self._all_levels = sorted(list(set(self._all_levels)))
        
        self._session_profile = {}
        self._entered_bar_timestamps = set()

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

    def _extract_major_levels(self, bars) -> list[float]:
        """Extract major levels from a sequence of bars."""
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
        
        return self._extract_major_levels_from_profile(profile)

    def _extract_major_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract only the MAJOR levels (top 30% by volume)."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Get only top levels comprising 30% of volume (more selective)
        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.30:
                break

        return sorted(list(set(top_levels)))

    def _detect_extreme_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect EXTREME absorption at MAJOR levels.
        
        This is much more selective than previous versions:
        1. Delta ratio must be 0.70+ (extreme, not just strong)
        2. Wick must be 0.25+ points (very visible rejection)
        3. Failed extension (close back past prior extreme)
        4. Price within 0.50 of a MAJOR level (top 30%)
        5. Target level exists 3.0+ points away (multi-session move)
        
        The goal: fewer, more structural trades that don't match baseline frequency.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Requirement 1: EXTREME one-sided flow (0.70+, not 0.50)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Requirement 2: VERY VISIBLE wick (0.25+, not 0.15)
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

        # Requirement 3: Failed extension
        if delta > 0:  # Buying pressure
            if current.close >= prior.low:
                return None
        elif delta < 0:  # Selling pressure
            if current.close <= prior.high:
                return None

        # Requirement 4: Price at a MAJOR level
        if not self._all_levels:
            return None

        closest_level: Optional[float] = None
        min_distance = float('inf')
        for level in self._all_levels:
            distance = abs(current.close - level)
            if distance < min_distance:
                closest_level = level
                min_distance = distance

        if closest_level is None or min_distance > self.level_proximity:
            return None

        # Requirement 5: Target level far enough away (3.0+ points)
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Find target level above
            above_levels = [l for l in self._all_levels if l > current.close + 0.5]
            if not above_levels:
                return None
            
            target_level = min(above_levels)
            target_distance = target_level - current.close
            
            if target_distance < self.min_target_points:
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
            below_levels = [l for l in self._all_levels if l < current.close - 0.5]
            if not below_levels:
                return None
            
            target_level = max(below_levels)
            target_distance = current.close - target_level
            
            if target_distance < self.min_target_points:
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on extreme absorption detection."""
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
