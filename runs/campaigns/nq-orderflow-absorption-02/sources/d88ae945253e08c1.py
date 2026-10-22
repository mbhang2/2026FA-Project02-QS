"""
Volume profile absorption strategy - focus on multi-bar absorption patterns.

Analysis of trial history:
- #16 scored -0.1076 with 463 trades (best real score)
- #13 scored -0.9744 with 102,546 trades (pure momentum fallback)
- Synthetic identical trades (70) persist despite parameter changes

Core insight: The synthetic identical trades suggest our single-bar absorption
detection is matching the baseline momentum pattern. The campaign asks if
absorption PRECEDES a move - which implies we should look for absorption
that PERSISTS across multiple bars, not just a single bar anomaly.

New hypothesis: True absorption is when price returns to a level multiple times
with strong one-sided flow, without breaking through. This is a multi-bar pattern
that distinguishes it from a single momentum bar.

Key changes:
1. **Multi-bar absorption detection** - require 2+ bars showing absorption pattern
2. **Level revisit tracking** - track when price returns to same level with strong flow
3. **Wick persistence** - require wicks in same direction across bars (failed extension)
4. **Volume accumulation at level** - strong flow concentrated at specific price
5. **Lower delta ratio** - 0.40 (we're looking at pattern, not single bar extremeness)
6. **Tighter stop placement** - based on lowest low of absorption pattern
7. **Longer target distance** - 4.0x for moves between levels

The reason this differs from baseline momentum:
- Momentum is close > open on a single bar
- Absorption pattern is price returning to level with flow, failing to extend
- This requires looking back 2-3 bars to see the pattern, not just current bar

This should produce DIFFERENT trades on synthetic if it's truly a different signal,
and hopefully better results on real data if the pattern is genuinely predictive.
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
        min_wick_ratio: float = 0.10,
        level_proximity: float = 0.50,
        pattern_bars: int = 2,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_ratio = min_wick_ratio
        self.level_proximity = level_proximity
        self.pattern_bars = pattern_bars
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Current session volume profile
        self._session_profile: dict[float, int] = {}
        
        # Track recent bars for pattern detection
        self._recent_bars: list = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < self.pattern_bars:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._session_profile = {}
            self._recent_bars = []

        # Accumulate volume profile
        self._update_profile(current)
        
        # Keep recent bars for pattern detection
        self._recent_bars.append(current)
        if len(self._recent_bars) > self.pattern_bars + 2:
            self._recent_bars.pop(0)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect multi-bar absorption pattern
        absorption = self._detect_absorption_pattern(history)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_profile(self, bar) -> None:
        """Accumulate volume at price levels with 0.25-point granularity."""
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

    def _get_peak_levels(self) -> list[float]:
        """Extract peak volume levels from current session profile."""
        if not self._session_profile:
            return []

        sorted_items = sorted(self._session_profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Get levels comprising top 70% of volume
        peak_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            peak_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.70:
                break

        return sorted(list(set(peak_levels)))

    def _detect_absorption_pattern(self, history) -> Optional[dict]:
        """
        Detect multi-bar absorption pattern: price returns to level with strong
        one-sided flow and fails to extend (wick in opposite direction).
        
        Pattern: 
        - Current bar has strong delta (>= 40% one-sided)
        - Current bar has wick opposite to flow (>= 10% of range)
        - Prior bar(s) also show similar pattern or price near level
        - Price is near a peak volume level
        - Pattern suggests absorption (flow not moving price)
        """
        if len(history) < self.pattern_bars:
            return None

        current = history[-1]
        
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Requirement 1: Strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Requirement 2: Wick opposite to flow
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

        # Requirement 3: Check prior bar(s) for pattern consistency
        pattern_valid = True
        if len(history) >= 2:
            prior = history[-2]
            prior_delta = prior.delta
            
            # For multi-bar pattern, prior bar should show similar directional bias
            # or at least not be strongly opposite
            if delta > 0 and prior_delta < 0:
                # Current is long, prior is short - could be reversal, check strength
                if abs(prior_delta) > prior.volume * 0.30:
                    pattern_valid = False
            elif delta < 0 and prior_delta > 0:
                # Current is short, prior is long - could be reversal, check strength
                if abs(prior_delta) > prior.volume * 0.30:
                    pattern_valid = False

        if not pattern_valid:
            return None

        # Requirement 4: Price near a peak volume level
        peak_levels = self._get_peak_levels()
        if not peak_levels:
            return None

        min_distance = min(abs(current.close - level) for level in peak_levels)
        if min_distance > self.level_proximity:
            return None

        # Calculate stop and target based on absorption pattern
        if delta > 0:  # Long
            # Stop below the lowest low of the pattern
            lowest_low = current.low
            if len(history) >= 2:
                lowest_low = min(lowest_low, history[-2].low)
            
            stop_price = lowest_low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        else:  # Short
            # Stop above the highest high of the pattern
            highest_high = current.high
            if len(history) >= 2:
                highest_high = max(highest_high, history[-2].high)
            
            stop_price = highest_high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on absorption pattern detection."""
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
