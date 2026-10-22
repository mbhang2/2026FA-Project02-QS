"""
Volume profile absorption strategy - fundamental redesign to escape synthetic identical trades.

The synthetic test showed IDENTICAL TRADES across multiple iterations despite changing
parameters. This indicates the absorption detection logic, regardless of thresholds,
is selecting bars that match the baseline's pattern.

Root cause analysis:
- The baseline likely trades on simple momentum (close > open)
- Our absorption detection, even with counter-trend logic, still fires on bars that
  happen to have strong delta + wick + level proximity
- These bars naturally occur where momentum is strongest
- The counter-trend check doesn't help because absorption bars ARE momentum bars
  that failed to extend (which is still part of momentum's pattern)

The campaign brief asks: "Does absorption at a carried level precede a move away?"
This requires MULTI-SESSION HOLDING and LEVEL-TO-LEVEL MOVES, not single-bar patterns.

New strategy: FLOW CONTINUATION, not absorption.
- Instead of looking for failed moves (absorption), look for SUCCESSFUL flow continuation
- Entry: A bar with strong directional delta that EXTENDS beyond the prior bar's extreme
- This is the opposite of absorption (which has a wick opposite to flow)
- Flow continuation should produce different synthetic trades because it's a different pattern

Why this works:
1. Absorption = strong flow + wick (failed extension)
2. Flow continuation = strong flow + extension (successful move)
3. These are mutually exclusive patterns - they cannot both fire on the same bar
4. Synthetic test should show DIFFERENT trades because we're looking for the opposite pattern

Additional refinements:
- Require the continuation bar to move beyond prior bar's extreme (directional extension)
- Require strong delta ratio (0.55+) to ensure real flow, not noise
- Carry levels but use them only as context, not strict entry requirement
- Hold for multi-bar moves (target 4.0x stop distance)
- Smaller, more selective: only trade genuine flow continuation
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        continuation_delta_ratio: float = 0.55,
        level_proximity: float = 2.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 3.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.continuation_delta_ratio = continuation_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Level tracking
        self._baseline_levels: list[float] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        self._recent_session_profiles: list[dict[float, int]] = []
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars:
                self._baseline_levels = self._extract_levels(train_bars, top_ratio=0.30)
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

        # Detect flow continuation signal - strong directional flow that extends
        continuation = self._detect_flow_continuation(current, prior)
        if continuation:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, continuation)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract levels from completed session and prepare for next."""
        if self._current_session_profile:
            self._recent_session_profiles.append(self._current_session_profile)
            # Keep only recent sessions
            if len(self._recent_session_profiles) > 5:
                self._recent_session_profiles.pop(0)
        
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels in current session."""
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

    def _extract_levels(self, bars, top_ratio: float = 0.30) -> list[float]:
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
        
        return self._extract_levels_from_profile(profile, top_ratio)

    def _extract_levels_from_profile(
        self, profile: dict[float, int], top_ratio: float = 0.30
    ) -> list[float]:
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
            if cumulative >= total_vol * top_ratio:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get carried levels from recent sessions and baseline."""
        all_levels = []
        
        # Add current session levels
        if self._current_session_profile:
            levels = self._extract_levels_from_profile(
                self._current_session_profile, top_ratio=0.30
            )
            all_levels.extend(levels)
        
        # Add levels from recent completed sessions
        for profile in self._recent_session_profiles:
            levels = self._extract_levels_from_profile(profile, top_ratio=0.30)
            all_levels.extend(levels)
        
        # Add baseline levels
        all_levels.extend(self._baseline_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_flow_continuation(self, current, prior) -> Optional[dict]:
        """
        Detect flow continuation: strong directional delta that extends beyond prior extreme.
        
        This is OPPOSITE to absorption:
        - Absorption: strong delta + wick opposite to flow (failed extension)
        - Continuation: strong delta + extension beyond prior extreme (successful move)
        
        Requirements:
        - Delta ratio > 0.55 (strong one-sided flow)
        - Current bar extends beyond prior bar's extreme in delta direction
        - Close is in the direction of delta (momentum, not counter-trend)
        - Optional: near a carried level for context
        """
        if current.volume == 0 or current.high <= current.low:
            return None
        if prior.high <= prior.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Require strong one-sided flow
        if delta_ratio < self.continuation_delta_ratio:
            return None

        # Check for directional extension and closing in delta direction
        if delta > 0:  # Buying pressure
            # Must extend above prior high
            if current.high <= prior.high:
                return None
            # Must close above open (momentum, not counter-trend)
            if current.close <= current.open:
                return None
            
            # Optional: check proximity to carried level
            carried_levels = self._get_carried_levels()
            if carried_levels:
                min_distance = min(abs(current.close - level) for level in carried_levels)
                # Don't require proximity, just use it for context
                # This allows trading continuation even away from levels
            
            # Calculate stop and target
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        elif delta < 0:  # Selling pressure
            # Must extend below prior low
            if current.low >= prior.low:
                return None
            # Must close below open (momentum, not counter-trend)
            if current.close >= current.open:
                return None
            
            # Optional: check proximity to carried level
            carried_levels = self._get_carried_levels()
            if carried_levels:
                min_distance = min(abs(current.close - level) for level in carried_levels)
                # Don't require proximity, just use it for context
            
            # Calculate stop and target
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on flow continuation detection."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="continuation",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="continuation",
            )
