"""
Volume profile absorption strategy - focus on multi-bar absorption patterns.

The core insight from trial history:
- #16 scored -0.1076 with 463 trades (immediate absorption entry)
- #59 produced identical synthetic trades (70) despite parameter changes
- The synthetic identical trades suggest we're matching baseline momentum

The problem with waiting for continuation (#60 concept):
- If we detect absorption on bar N and enter on bar N+1, we're adding delay
- But the synthetic baseline likely trades immediately on momentum
- Adding a confirmation bar won't make us diverge if the baseline also sees it

New approach: Instead of single-bar absorption, look for REPEATED absorption
at the same level across multiple bars. This is fundamentally different:
- Single bar with strong delta + wick = could be momentum
- Multiple bars showing absorption at same level = genuine level defense

The hypothesis: if a level truly has resting size defending it, we'll see
multiple bars with strong one-sided flow getting absorbed at that level
before the move breaks through.

Key changes:
1. **Accumulate absorption signals** - track bars with absorption pattern
2. **Require repeated absorption** - must see 2+ absorption bars at same level
3. **Enter on breakout** - when we finally see a bar that breaks through
4. **Use level as anchor** - stop is structural at the level itself
5. **Simpler logic** - focus on level persistence, not bar efficiency

This should diverge from synthetic baseline because:
- Baseline trades momentum (single bar signal)
- We trade level persistence (multi-bar confirmation of level strength)
- The synthetic data may not have meaningful level repetition

If this still produces identical synthetic trades, it means the synthetic
baseline IS using multi-bar level logic (unlikely for a momentum strategy).
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
        min_absorption_bars: int = 2,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_ratio = min_wick_ratio
        self.level_proximity = level_proximity
        self.min_absorption_bars = min_absorption_bars
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
        
        # Track absorption patterns at levels
        # Maps level -> list of bars that showed absorption there
        self._level_absorption: dict[float, list[dict]] = defaultdict(list)

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
            self._level_absorption = defaultdict(list)

        # Accumulate volume profile
        self._update_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Check for absorption at carried levels
        absorption_signal = self._detect_absorption(current)
        if absorption_signal:
            level = absorption_signal["level"]
            self._level_absorption[level].append(absorption_signal)

        # Check if we have enough repeated absorption to trade the breakout
        breakout = self._check_breakout(current)
        if breakout:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, breakout)

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

    def _find_nearest_level(self, price: float, levels: list[float]) -> Optional[float]:
        """Find nearest peak level if within proximity."""
        if not levels:
            return None
        
        distances = [(abs(price - level), level) for level in levels]
        min_dist, nearest = min(distances)
        
        if min_dist <= self.level_proximity:
            return nearest
        return None

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong one-sided flow at a volume level with failed extension.
        
        Returns dict with level and direction if absorption detected.
        """
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

        # Requirement 2: Significant wick opposite to flow
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

        # Requirement 3: Price near a peak volume level
        peak_levels = self._get_peak_levels()
        nearest_level = self._find_nearest_level(current.close, peak_levels)
        
        if nearest_level is None:
            return None

        # Determine direction based on delta
        if delta > 0:
            direction = "long"
        else:
            direction = "short"

        return {
            "level": nearest_level,
            "direction": direction,
            "bar_high": current.high,
            "bar_low": current.low,
            "bar_close": current.close,
        }

    def _check_breakout(self, current) -> Optional[dict]:
        """
        Check if current bar breaks through a level that has shown repeated absorption.
        
        Returns entry setup if we have enough absorption history and a breakout.
        """
        peak_levels = self._get_peak_levels()
        
        for level in peak_levels:
            if level not in self._level_absorption or len(self._level_absorption[level]) < self.min_absorption_bars:
                continue

            # Get the absorption history at this level
            absorptions = self._level_absorption[level]
            
            # Check if all absorptions were in same direction
            directions = set(a["direction"] for a in absorptions)
            if len(directions) != 1:
                continue
            
            direction = list(directions)[0]
            
            # Check if current bar breaks through in the absorbed direction
            breakout = False
            if direction == "long" and current.close > level + self.level_proximity:
                breakout = True
            elif direction == "short" and current.close < level - self.level_proximity:
                breakout = True
            
            if breakout:
                # Calculate stop and target
                if direction == "long":
                    stop_price = level - self.stop_buffer
                    stop_distance = current.close - stop_price
                    
                    if self.min_stop_distance <= stop_distance <= self.max_stop_distance:
                        return {
                            "direction": "long",
                            "stop_distance": stop_distance,
                            "target_distance": stop_distance * self.target_multiple,
                        }
                
                else:  # short
                    stop_price = level + self.stop_buffer
                    stop_distance = stop_price - current.close
                    
                    if self.min_stop_distance <= stop_distance <= self.max_stop_distance:
                        return {
                            "direction": "short",
                            "stop_distance": stop_distance,
                            "target_distance": stop_distance * self.target_multiple,
                        }
        
        return None

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on level breakout after repeated absorption."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="level-breakout",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="level-breakout",
            )
