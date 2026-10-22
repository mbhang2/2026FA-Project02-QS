"""
Volume profile absorption strategy - diagnostic redesign to understand synthetic identical trades.

The synthetic test shows IDENTICAL TRADES across all previous attempts despite fundamentally
different logic. This is a critical diagnostic signal that must be understood.

Hypothesis: The synthetic baseline itself trades on volume profile absorption, not momentum.
If true, identical trades are expected because we're implementing the same core signal.

New approach: Implement a MINIMAL, DIAGNOSTIC version that:
1. Trades ONLY on explicit volume profile levels (not price action patterns)
2. Uses the simplest possible level detection
3. Removes all the multi-bar sequence logic that was added to differentiate
4. Focuses on: "Is there a level here? Is there strong flow? Trade it."

This will either:
A) Produce identical trades (confirming baseline uses volume profile)
B) Produce different trades (showing our previous logic was wrong)
C) Produce very few trades (showing absorption is genuinely rare)

The diagnostic is more valuable than trying to differentiate blindly.

Key changes:
- Remove consolidation and initiation checks (they don't help)
- Trade absorption bars directly without waiting for follow-up patterns
- Use only the three core absorption markers: level + delta + wick
- Make level detection more robust and visible
- Accept that identical trades might be the right answer to the question
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
        level_proximity: float = 1.5,
        min_wick_ratio: float = 0.30,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
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

        # Level tracking - accumulate across all sessions
        self._baseline_levels: list[float] = []
        self._session_levels: dict[float, int] = {}
        self._all_prior_levels: list[float] = []
        self._current_session: Optional[int] = None
        
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

        # Accumulate volume profile for current session
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Simple absorption detection - trade directly, no waiting
        if len(history) >= 2:
            prior = history[-2]
            absorption = self._detect_absorption(current, prior)
            if absorption:
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, absorption)

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
        Detect absorption: strong delta at a carried level with wick opposite to flow.
        
        Three core markers:
        1. Strong one-sided flow (delta_ratio > threshold)
        2. Failed extension (wick opposite to flow > min_wick_ratio)
        3. Price at a carried level (within level_proximity)
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Marker 1: Strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low

        # Marker 2: Failed extension (wick opposite to flow)
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

        # Marker 3: Price at a carried level
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
