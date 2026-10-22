"""
Volume profile absorption strategy - addressing synthetic identical trades through
fundamental signal redesign.

The synthetic identical trades problem reveals that our absorption detection,
despite complexity, is selecting bars that match the baseline's pattern. This
suggests we're using filters that accidentally correlate with momentum.

Key insight: the campaign brief emphasizes "flow that FAILS to move price" and
"moves BETWEEN levels" (multi-bar, multi-session). We've been detecting absorption
at a single bar, but the real signal might be SUSTAINED absorption across multiple
bars at a level.

New approach - detect MULTI-BAR ABSORPTION PATTERN:
1. A level must show absorption across 2-3 consecutive bars
2. Each bar needs strong delta (0.50+) opposite to prior bar's direction
3. Volume must increase or sustain (not declining absorption)
4. Only after this pattern forms do we enter on the next breakout
5. This creates a fundamentally different entry signal than single-bar momentum

Why this changes synthetic trades:
- Momentum: single bar with close > open
- Multi-bar absorption: pattern of 2+ bars with alternating/sustained pressure
- The baseline momentum strategy won't match a multi-bar pattern detector

This also aligns with the campaign's emphasis on:
- Moves between levels (we hold for multi-bar patterns before entering)
- Structural signal (absorption pattern is more structural than single bar)
- Lower frequency (multi-bar patterns occur less often than single bars)

If synthetic test still shows identical trades, it means the baseline itself
uses multi-bar logic, which would be surprising and informative.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.50,
        level_proximity: float = 0.75,
        min_wick_points: float = 0.10,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_distance: float = 3.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        pattern_bars: int = 2,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.pattern_bars = pattern_bars

        # Level tracking
        self._baseline_levels: list[float] = []
        self._session_profile: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._all_prior_levels: list[float] = []
        
        # Track absorption patterns
        self._bar_history: deque = deque(maxlen=10)
        self._absorption_patterns: dict[float, int] = {}
        
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
        if len(history) < self.pattern_bars + 1:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            self._on_session_change()
            self._current_session = current.session

        # Accumulate volume profile
        self._update_session_profile(current)
        
        # Track bar for pattern detection
        self._bar_history.append(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect multi-bar absorption pattern
        absorption = self._detect_multi_bar_absorption()
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract levels from completed session."""
        if self._session_profile:
            levels = self._extract_levels_from_profile(self._session_profile)
            self._all_prior_levels.extend(levels)
            self._all_prior_levels = sorted(list(set(self._all_prior_levels)))
        
        self._session_profile = {}
        self._absorption_patterns = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
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

        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.70:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_profile))
        all_levels.extend(self._all_prior_levels)
        all_levels.extend(self._baseline_levels)
        return sorted(list(set(all_levels)))

    def _detect_multi_bar_absorption(self) -> Optional[dict]:
        """
        Detect multi-bar absorption pattern: 2+ bars with strong opposing deltas
        at a carried level, followed by a breakout bar.
        
        This is fundamentally different from single-bar momentum detection.
        """
        if len(self._bar_history) < self.pattern_bars + 1:
            return None

        # Get the last pattern_bars bars and the current bar
        bars = list(self._bar_history)
        current = bars[-1]
        pattern_bars = bars[-(self.pattern_bars + 1):-1]

        # Check if we have a multi-bar absorption pattern
        if not self._is_absorption_pattern(pattern_bars, current):
            return None

        # Find the level where absorption occurred
        pattern_level = self._find_pattern_level(pattern_bars)
        if pattern_level is None:
            return None

        # Verify level is carried
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(pattern_level - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Determine entry direction based on pattern breakout
        delta = current.delta
        
        if delta > 0:  # Breakout up after absorption
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        elif delta < 0:  # Breakout down after absorption
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

    def _is_absorption_pattern(self, pattern_bars: list, current) -> bool:
        """
        Check if pattern_bars show absorption characteristics:
        - Strong deltas (0.50+)
        - Alternating or sustained pressure
        - Wicks opposite to flow
        """
        if len(pattern_bars) < self.pattern_bars:
            return False

        strong_deltas = 0
        for bar in pattern_bars:
            if bar.volume == 0:
                return False
            
            delta_ratio = abs(bar.delta) / bar.volume
            if delta_ratio >= self.absorption_delta_ratio:
                strong_deltas += 1

        # Need most bars to have strong delta
        if strong_deltas < len(pattern_bars) - 1:
            return False

        # Check that current bar has opposite or sustained delta
        if len(pattern_bars) > 0:
            last_pattern_delta = pattern_bars[-1].delta
            current_delta = current.delta
            
            # Should be opposite direction (one positive, one negative)
            if last_pattern_delta * current_delta > 0:
                # Same direction - only allow if both are strong
                if abs(current.delta) / max(current.volume, 1) < self.absorption_delta_ratio:
                    return False

        return True

    def _find_pattern_level(self, pattern_bars: list) -> Optional[float]:
        """Find the price level where absorption pattern occurred."""
        if not pattern_bars:
            return None

        # Use the average close of the pattern as the level
        closes = [bar.close for bar in pattern_bars]
        return sum(closes) / len(closes)

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on multi-bar absorption pattern."""
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
