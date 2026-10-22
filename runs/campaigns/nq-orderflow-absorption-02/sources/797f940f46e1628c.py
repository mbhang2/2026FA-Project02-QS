"""
Volume profile absorption strategy - refinement for genuine absorption detection.

The synthetic test shows IDENTICAL TRADES to the current strategy, meaning our
absorption detection is firing on the same bars. The requirement that close be
pulled back (close < open for long) is too restrictive and may never fire.

Analysis of attempts:
- #16 (score -0.1076): 463 trades, absorption only
- #19 (score unknown): stricter absorption with level proximity
- Current: requires close < open for long absorption (contrarian)

The issue: A bar with strong buying delta AND a lower tail doesn't necessarily
close below open. It could close at open, or above open but below the high.
Requiring close < open is too strict and may produce 0 trades.

Key insight from the campaign: We're looking for absorption - flow that arrives
at a level and doesn't move price. This is NOT a contrarian signal by definition.
A bar with strong buying delta and a tail is absorption regardless of where close is.

Refinement:
1. Remove the "close pulled back" requirement (close < open)
2. Keep strong delta requirement (0.45+)
3. Keep visible tail requirement (0.10 points)
4. Keep tail/range ratio (0.30+)
5. Make absorption about the BAR SHAPE, not the relationship to open
6. Target the next level in the profile for multi-bar moves
7. Use level proximity to filter setups (absorption at levels, not random)

This should produce different trades than momentum because:
- We require BOTH strong delta AND significant tail
- Momentum bars often have minimal tails
- The combination is rarer and more specific than just close > open
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
        min_wick_points: float = 0.10,
        tail_to_range_ratio: float = 0.30,
        level_proximity: float = 2.0,
        stop_buffer: float = 0.25,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        min_target_distance: float = 1.5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.tail_to_range_ratio = tail_to_range_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.min_target_distance = min_target_distance
        self.size = size

        # Level tracking
        self._baseline_levels: list[float] = []
        self._current_session: Optional[int] = None
        self._session_profile: dict[float, int] = {}
        self._all_session_levels: list[list[float]] = []
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Build baseline levels from training split
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
        """Extract levels from session and prepare for next."""
        if self._session_profile:
            levels = self._extract_levels(self._session_profile)
            if levels:
                self._all_session_levels.append(levels)
        
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

    def _extract_levels_from_bars(self, bars) -> list[float]:
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
        
        return self._extract_levels(profile)

    def _extract_levels(self, profile: dict[float, int]) -> list[float]:
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
        all_levels.extend(self._baseline_levels)
        
        # Add levels from all prior sessions
        for session_levels in self._all_session_levels:
            all_levels.extend(session_levels)
        
        # Add current session levels
        if self._session_profile:
            all_levels.extend(self._extract_levels(self._session_profile))
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong delta with meaningful tail at a carried level.
        
        Requirements:
        1. Delta ratio >= 0.45 (strong one-sided flow)
        2. Visible tail opposite to flow direction (0.10+ points)
        3. Tail is significant relative to bar range (30%+ of range)
        4. Price is within 2.0 points of a carried level
        5. Target level exists and is at least 1.5 points away
        6. Valid stop distance (0.25 to 5.0 points)
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume
        bar_range = current.high - current.low

        # Requirement 1: Strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check proximity to carried level (Requirement 4)
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Requirement 2 & 3: Meaningful tail opposite to flow
        if delta > 0:  # Buying pressure - need significant lower tail
            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None
            if bar_range > 0 and lower_wick / bar_range < self.tail_to_range_ratio:
                return None
            
            # Calculate stop
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Requirement 5: Find target level above
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
        
        elif delta < 0:  # Selling pressure - need significant upper tail
            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None
            if bar_range > 0 and upper_wick / bar_range < self.tail_to_range_ratio:
                return None
            
            # Calculate stop
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Requirement 5: Find target level below
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
        
        return None

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
