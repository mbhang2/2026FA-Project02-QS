"""
Volume profile absorption strategy - refocus on the core hypothesis.

The synthetic identical trades issue reveals a fundamental problem: we've been
trying to detect absorption through increasingly complex filters, but the
synthetic baseline apparently produces the same trades regardless.

Key insight from reviewing the campaign: the question asks about MULTI-SESSION
HOLDING and LEVEL-TO-LEVEL MOVES. Trial #16 scored -0.1076 with only 463 trades
across 384 sessions - that's ~1.2 trades per session on average, with multi-bar
holding periods.

The synthetic identical trades suggest our absorption detection is either:
1. Never firing (too restrictive)
2. Accidentally selecting the same bars as baseline
3. Being overridden by some hidden fallback

New approach: SIMPLIFY and FOCUS on the actual campaign hypothesis.

The campaign asks: "Does absorption at a carried level precede a move away?"

This requires:
- Carried levels from prior sessions (not just current session)
- Absorption detection that is ACTUALLY DIFFERENT from momentum
- Multi-bar target (moves between levels, not single-bar scalps)
- Holding positions for real moves, not quick exits

The counter-trend filter (close opposite to delta) was too restrictive and
probably never fires. Instead, focus on:
1. Strong directional flow (delta_ratio > 0.50)
2. Proximity to a carried level (within 1.0 point)
3. Wick opposite to flow (any amount > 0) - simple, not ratio-based
4. Multi-session level carryover (the actual campaign requirement)
5. Realistic target sizing (3.0x stop for level-to-level moves)

Remove the counter-trend requirement entirely. Absorption can happen WITH momentum
- it's about flow being absorbed at a level, not about the direction of the close.

The reason #39 with flow continuation may have failed: it required close > open
for longs and close < open for shorts, which is momentum. But the campaign asks
about absorption, which is different.

Let's return to pure absorption detection without the counter-trend filter, but
with proper multi-session level carryover that was missing before.
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
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Multi-session level tracking
        self._baseline_levels: list[float] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        self._prior_session_profiles: list[dict[float, int]] = []
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()

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

        # Detect absorption at a carried level
        absorption = self._detect_absorption(current)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Move current session profile to prior sessions, reset for new session."""
        if self._current_session_profile:
            self._prior_session_profiles.append(self._current_session_profile)
            # Keep last 3 sessions of profiles
            if len(self._prior_session_profiles) > 3:
                self._prior_session_profiles.pop(0)
        
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels in current session."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Use 0.25-point granularity
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
        """Get all carried levels from prior sessions and baseline."""
        all_levels = []
        
        # Add levels from prior completed sessions
        for profile in self._prior_session_profiles:
            levels = self._extract_levels_from_profile(profile)
            all_levels.extend(levels)
        
        # Add baseline levels
        all_levels.extend(self._baseline_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong directional flow at a carried level with wick opposite to flow.
        
        Absorption = price arrives at a carried level + strong one-sided flow + failed extension
        
        This is the core campaign hypothesis: absorption at a level precedes a move away.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Require strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for wick opposite to flow direction
        has_wick = False
        if delta > 0:  # Buying pressure
            lower_wick = current.close - current.low
            if lower_wick > 0:
                has_wick = True
        elif delta < 0:  # Selling pressure
            upper_wick = current.high - current.close
            if upper_wick > 0:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to carried level
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
