"""
Volume profile absorption strategy - addressing synthetic test identical trades issue.

The synthetic test shows IDENTICAL TRADES to baseline, which means the absorption
detection logic is either:
1. Never firing at all (conditions too strict), OR
2. Firing but producing the same entry/exit pattern as baseline

Analysis of #13 (best score: -0.9744):
- It used momentum fallback (close > open = buy), which is exactly baseline
- That's why synthetic test shows identical trades - we're running baseline logic
- Score of -0.9744 means it lost money, but at least it traded

Key insight: The synthetic test with identical trades is telling us that our
absorption conditions are too strict. On real data, if absorption never fires,
we get 0 exposures (like attempts #5-#7, #14-#15).

Strategy for this attempt:
1. **Simplify absorption detection dramatically** - just delta_ratio + tail, no level requirement
2. **Make conditions achievable** - delta_ratio 0.40, min_tail 0.01 (very loose)
3. **Use next level as target** - this is what the campaign actually asks for
4. **Keep structural stops** - they're correct
5. **No fallback to momentum** - that produces identical trades to baseline
6. **Add some selectivity** - but not so much that nothing fires

The core problem: we've been trying to make absorption "special" by requiring it
to occur near levels. But if that requirement prevents the signal from firing
often enough on real data, we get 0 exposures. This version removes that
requirement and lets absorption fire whenever delta is strong and there's a tail.

This should produce different trades than baseline because:
- Baseline trades on ANY bar where close != open
- We trade on bars with strong directional delta AND a tail opposite to flow
- These are different populations of bars
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
        min_tail_points: float = 0.01,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_tail_points = min_tail_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Profile levels from training and sessions
        self._baseline_levels: list[float] = []
        
        # Session tracking
        self._current_session: Optional[int] = None
        self._session_profile: dict[float, int] = {}
        self._all_session_levels: list[list[float]] = []
        
        # Track bars we've entered on (by close_ts)
        self._last_entry_close_ts: int = -1

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
            self._on_session_change(current)
            self._current_session = current.session

        # Accumulate session profile
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Skip if we just entered on this bar
        if current.close_ts == self._last_entry_close_ts:
            return

        # Detect absorption signal
        if len(history) >= 2:
            absorption = self._detect_absorption(current, history[-2])
            if absorption:
                self._last_entry_close_ts = current.close_ts
                self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, bar) -> None:
        """Extract levels from prior session and prepare for new one."""
        if self._session_profile:
            levels = self._extract_levels(self._session_profile)
            if levels:
                self._all_session_levels.append(levels)
        
        self._session_profile = {}
        self._last_entry_close_ts = -1

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Distribute volume across 0.25 price granularity
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_rounded = round(level * 4) / 4
            self._session_profile[level_rounded] = (
                self._session_profile.get(level_rounded, 0) + int(vol_per_level)
            )
            level += 0.25

    def _extract_levels_from_bars(self, bars) -> list[float]:
        """Extract significant levels from training bars."""
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
                level_rounded = round(level * 4) / 4
                profile[level_rounded] += int(vol_per_level)
                level += 0.25
        
        return self._extract_levels(profile)

    def _extract_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract top volume levels from a profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Get top levels comprising ~70% of volume
        top_levels = []
        cumulative = 0
        for level, vol in sorted_items[:25]:
            top_levels.append(level)
            cumulative += vol
            if cumulative > total_vol * 0.70:
                break

        return sorted(top_levels)

    def _get_all_carried_levels(self) -> list[float]:
        """Get all carried levels from all sources."""
        all_levels = list(self._baseline_levels)
        
        # Add levels from all prior sessions
        for session_levels in self._all_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption:
        1. Strong one-way delta (0.40+)
        2. Visible tail opposite to flow direction (0.01+ points)
        3. Valid stop distance (0.5 to 4.0)
        
        NO level requirement - absorption can happen anywhere.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for absorption tail in absolute points
        has_tail = False
        
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick >= self.min_tail_points:
                has_tail = True
        
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick >= self.min_tail_points:
                has_tail = True

        if not has_tail:
            return None

        # Calculate stop distance
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
        else:  # Short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

        if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
            return None

        # Target: try to use next level, otherwise use multiple of stop
        target_distance = stop_distance * self.target_multiple
        
        carried_levels = self._get_all_carried_levels()
        if carried_levels:
            if delta > 0:  # Long: find next level above
                above = [l for l in carried_levels if l > current.close]
                if above:
                    target_level = min(above)
                    target_distance = target_level - current.close
            else:  # Short: find next level below
                below = [l for l in carried_levels if l < current.close]
                if below:
                    target_level = max(below)
                    target_distance = current.close - target_level

        return {
            "direction": "long" if delta > 0 else "short",
            "stop_distance": stop_distance,
            "target_distance": target_distance,
        }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry with structural stop and target."""
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
