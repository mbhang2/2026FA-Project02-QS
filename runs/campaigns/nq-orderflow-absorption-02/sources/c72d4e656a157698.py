"""
Volume profile absorption strategy - refinement targeting improvement on #76's -0.0893.

Analysis of campaign performance:
- #76 scored -0.0893 with 1079 trades (BEST REAL SCORE)
- #74 scored -0.0980 with 2763 trades
- #16 scored -0.1076 with 463 trades
- Synthetic baseline: consistently 70 trades (no volume structure in random data)

The current strategy produces identical trades to #76 on synthetic data, meaning
parameter tweaks alone won't change behavior. However, the campaign brief provides
critical guidance that hasn't been fully incorporated:

1. "Absorption at a carried level precedes a move away from that level"
   - Current strategy checks proximity to levels but doesn't validate that a
     meaningful move actually follows
   - Solution: Add a FORWARD-LOOKING filter that checks if price has room to
     move to the next level before entering

2. "A rule that acts on absorption alone and a rule that waits for initiation
    are both answers"
   - Current strategy enters immediately on absorption
   - Solution: Track both entry types separately - absorption-only AND
     absorption+initiation, with different tags for measurement

3. The brief emphasizes the COST problem: "No signal that size survives this
   cost model at that frequency"
   - Current strategy at 1079 trades is still high frequency
   - Solution: Add a minimum move requirement - only enter if the distance to
     the next level is at least 4.0 points, ensuring we have room for profit
     after costs

4. "Stop sits just beyond the absorption bar's own extreme by a small buffer"
   - Current strategy uses 0.25 buffer, which is correct
   - But the target calculation (3.2x multiple) may not reach the next level
   - Solution: Ensure target_distance is calibrated to actually reach meaningful
     levels, not arbitrary multiples

Key changes for this trial:
- Add minimum distance to next level filter (4.0 points)
- This naturally reduces trade frequency while maintaining absorption detection
- The reduction comes from filtering for setups where we can actually reach the
  next level, not from arbitrary parameter tightening
- Keep absorption detection logic from #76 (which works) but add the forward
  validation that the campaign brief emphasizes
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.32,
        level_proximity: float = 4.0,
        min_wick_points: float = 0.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.2,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_levels_per_session: int = 4,
        exact_level_proximity: float = 1.0,
        min_distance_to_next_level: float = 4.0,
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
        self.max_levels_per_session = max_levels_per_session
        self.exact_level_proximity = exact_level_proximity
        self.min_distance_to_next_level = min_distance_to_next_level

        # Level tracking
        self._baseline_levels: list[float] = []
        self._session_levels: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._all_prior_levels: list[float] = []
        
        # Track bars we've entered on
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

        # Detect absorption with forward validation
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
        if self._session_levels:
            levels = self._extract_levels_from_profile(self._session_levels)
            self._all_prior_levels.extend(levels)
        
        self._session_levels = {}
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
        """Extract only the top N most significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        
        # Take only top N levels, not a percentile
        top_levels = [level for level, vol in sorted_items[:self.max_levels_per_session]]
        
        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        all_levels.extend(self._all_prior_levels)
        all_levels.extend(self._baseline_levels)
        return sorted(list(set(all_levels)))

    def _find_exact_level(self, price: float) -> Optional[float]:
        """Find if price is very close to an actual carried level."""
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None
        
        # Find the closest level
        closest = min(carried_levels, key=lambda l: abs(l - price))
        distance = abs(closest - price)
        
        # Must be very close to the actual level
        if distance <= self.exact_level_proximity:
            return closest
        
        return None

    def _find_next_level(self, price: float, direction: str) -> Optional[float]:
        """Find the next level in the direction of movement."""
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None
        
        if direction == "long":
            # Find closest level above current price
            above = [l for l in carried_levels if l > price]
            if above:
                return min(above)
        else:  # short
            # Find closest level below current price
            below = [l for l in carried_levels if l < price]
            if below:
                return max(below)
        
        return None

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with forward validation.
        Entry must be AT a carried level AND there must be room to next level.
        
        The campaign brief emphasizes: "absorption at a carried level precedes
        a move away from that level". This means we only enter if:
        1. We're at a carried level (absorption detection)
        2. There's a meaningful next level to move toward (forward validation)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Selective delta threshold
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require clear wick opposite to flow
        has_wick = False
        if delta > 0:  # Buying pressure
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Must be AT an actual carried level
        exact_level = self._find_exact_level(current.close)
        if exact_level is None:
            return None

        # NEW: Validate there's a next level with sufficient distance
        # This ensures we're not just detecting absorption, but absorption that
        # precedes a move to another level (as the campaign asks)
        direction = "long" if delta > 0 else "short"
        next_level = self._find_next_level(current.close, direction)
        
        if next_level is None:
            return None
        
        distance_to_next = abs(next_level - current.close)
        if distance_to_next < self.min_distance_to_next_level:
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
