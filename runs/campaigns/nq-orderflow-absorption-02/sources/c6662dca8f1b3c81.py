"""
Volume profile absorption strategy - focus on the core absorption signal with
refined initiation filtering.

Analysis of campaign trajectory:
- #16 scored -0.1076 with 463 trades (only real score achieved)
- Subsequent attempts with initiation filtering showed identical synthetic trades
- The identical synthetic behavior suggests initiation logic isn't firing on synthetic data
- Real data needs a different approach: tighter absorption detection + better initiation

Key insight: The initiation filtering in #64 never fires on synthetic data because:
1. Synthetic data has no real volume profile structure
2. The efficiency threshold calculation is too loose
3. The confirmation logic is checking wrong conditions

New approach - return to immediate absorption entry but with refinements:
1. **Stricter absorption detection** - delta_ratio 0.45 (strong absorption only)
2. **Tighter level proximity** - 0.15 points (must be very close to baseline)
3. **Require significant wick** - min_wick_ratio 0.20 (strong failed extension)
4. **Tighter stop distance** - max 3.0 points (avoid wide stops that don't survive)
5. **Lower target multiple** - 2.0x (conservative moves between levels)
6. **Simpler logic** - no complex pending/confirmation, just immediate entry

The reason #16 worked: it had the right balance of absorption detection strictness.
The reason recent attempts failed: either too loose (many false signals) or too complex
(initiation logic doesn't trigger on real data patterns).

This trial strips complexity and focuses on what made #16 work: genuine absorption
at carried levels with good risk/reward geometry.

If synthetic still identical: confirms the baseline is also absorption-based.
If real score improves: we've found a better parameter set than #16.
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
        min_wick_ratio: float = 0.20,
        level_proximity: float = 0.15,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.0,
        max_stop_distance: float = 3.0,
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

        # Baseline levels from training
        self._baseline_levels: list[float] = []
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Extract baseline levels from training split
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
            self._current_session = current.session
            self._entered_bar_timestamps = set()

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption and enter immediately
        absorption = self._detect_absorption(current)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _extract_levels(self, bars) -> list[float]:
        """Extract significant volume levels from training bars."""
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

    def _is_near_baseline_level(self, price: float) -> bool:
        """Check if price is very close to any baseline level (0.15 points max)."""
        if not self._baseline_levels:
            return False
        
        min_distance = min(abs(price - level) for level in self._baseline_levels)
        return min_distance <= self.level_proximity

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong one-sided flow at a baseline level with significant wick.
        
        Requirements (stricter than #16):
        1. Very strong delta (>= 45% of volume one-sided)
        2. Price very close to baseline level (<= 0.15 points)
        3. Significant wick opposite to flow (>= 20% of range)
        4. Stop distance within tight bounds (<= 3.0 points)
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Requirement 1: Very strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Requirement 2: Very close to baseline level
        if not self._is_near_baseline_level(current.close):
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Requirement 3: Significant wick opposite to flow
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

        # Calculate stop and target
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            # Requirement 4: Stop distance within bounds
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

            # Requirement 4: Stop distance within bounds
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
