"""
Volume profile absorption strategy - refined entry timing with dual-path testing.

Analysis of campaign results:
- #16 scored -0.1076 with 463 trades (best real score)
- Recent trials with confirmation/initiation logic produced identical synthetic trades
- The identical synthetic behavior suggests our absorption detection is sound
- The real data performance divergence suggests parameter tuning is key

Key insight: The initiation/confirmation approach in the previous trial likely
reduced signal too much by waiting for a second bar. The campaign question asks
about absorption at a level preceding a move BETWEEN levels, not about confirmation.

New approach - return to immediate absorption entry but with refined parameters:
1. **Tighter delta ratio (0.45)** - require stronger genuine absorption
2. **Looser level proximity (0.35 points)** - allow reasonable price drift from levels
3. **Moderate wick detection (0.15 range ratio)** - clear failed extension
4. **Structural stop** - just beyond absorption bar extreme + buffer
5. **Longer targets (3.5x stop)** - designed for multi-bar moves between levels
6. **Session-only levels** - keep current session profile simple
7. **Skip bars with weak wick** - avoid trades where absorption is ambiguous

The hypothesis: #16 worked because it balanced all constraints. Recent trials either
over-constrained (identical synthetic) or under-constrained (too many trades).
This trial tunes toward #16's sweet spot with slight adjustments to level proximity
(looser) and target multiple (longer) to better catch moves between levels.

If synthetic stays identical: the baseline and our absorption detection are
converging, but real data diverges due to parameter differences.
If real score improves: we've found a better balance than #16.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.45,
        min_wick_ratio: float = 0.15,
        level_proximity: float = 0.35,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
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

        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Current session volume profile
        self._session_profile: dict[float, int] = {}

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            self._current_session = current.session
            self._entered_bar_timestamps = set()
            self._session_profile = {}

        # Accumulate volume profile
        self._update_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption: strong delta at a volume level with wick opposite to flow
        absorption = self._detect_absorption(current)
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

        # Get levels comprising top 60% of volume
        peak_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            peak_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.60:
                break

        return sorted(list(set(peak_levels)))

    def _is_near_level(self, price: float, levels: list[float]) -> bool:
        """Check if price is near a peak volume level."""
        if not levels:
            return False
        min_distance = min(abs(price - level) for level in levels)
        return min_distance <= self.level_proximity

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong one-sided flow at a volume level with wick opposite to flow.
        
        Requirements:
        1. Strong delta (>= 45% of volume one-sided) - genuine absorption
        2. Wick opposite to flow (>= 15% of range) - failed extension
        3. Price near a peak volume level (<= 0.35 points)
        4. Stop distance within acceptable range
        
        Tuned toward #16's success with slightly looser level proximity (0.35 vs 0.20)
        and longer targets (3.5x vs 3.0x) to better capture moves between levels.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Requirement 1: Strong one-sided flow (45% threshold - tighter than #16's 40%)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Requirement 2: Wick opposite to flow (15% of range)
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

        # Requirement 3: Price near a peak volume level (0.35 points - looser than #16)
        peak_levels = self._get_peak_levels()
        if not self._is_near_level(current.close, peak_levels):
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
