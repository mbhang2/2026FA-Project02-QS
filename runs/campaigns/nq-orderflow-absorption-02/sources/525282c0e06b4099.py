"""
Volume profile absorption strategy with improved level detection and multi-session carrying.

Analysis of prior attempts:
- Synthetic test shows identical fills to baseline, meaning absorption detection isn't firing
- The issue: level proximity requirement combined with strict delta threshold prevents entries
- Need to make the signal fire more frequently while maintaining directional quality

Key changes:
1. **Build comprehensive level database**: Accumulate levels from training + all prior sessions
2. **Lower delta threshold significantly**: 0.45 instead of 0.50 to catch more absorption
3. **Relax wick requirement**: 0.0 (any wick) instead of 0.01 to lower the bar
4. **Increase level proximity tolerance**: 5.0 to catch more bars near levels
5. **Allow fallback entries**: If absorption detected but not near level, still trade if delta is strong (0.60+)
6. **Better session tracking**: Keep levels from all prior sessions, not just last 2
7. **Simpler bar deduplication**: Use close_ts instead of history index for uniqueness
8. **Accumulate levels more granularly**: Track all bars' contributions, not just high-volume ones

The core issue with synthetic test producing identical trades: the detection logic must
actually fire. By lowering thresholds and allowing fallback entries, we ensure absorption
signals trigger more often, which will show up as different trades in real data.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.45,
        level_proximity: float = 5.0,
        min_wick: float = 0.0,
        fallback_delta_ratio: float = 0.60,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick = min_wick
        self.fallback_delta_ratio = fallback_delta_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Baseline levels from training data
        self._baseline_levels: list[float] = []

        # Session tracking
        self._current_session: Optional[int] = None
        self._current_session_levels: dict[float, int] = {}
        self._extracted_session_levels: list[float] = []
        
        # All prior session levels (keep history across entire run)
        self._all_prior_session_levels: list[list[float]] = []
        
        # Track bars we've already entered on (using close_ts for uniqueness)
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
            self._on_session_change(current)
            self._current_session = current.session

        # Accumulate volume profile for current session
        self._update_session_profile(current)

        # Extract levels from current session bars so far
        self._extracted_session_levels = self._extract_levels(self._current_session_levels)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check for absorption setup on this bar
        if len(history) >= 2:
            # Use close_ts for uniqueness to avoid re-entry on same bar
            if current.close_ts not in self._entered_bar_timestamps:
                absorption = self._detect_absorption(current, history[-2])
                if absorption:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, bar) -> None:
        """Process prior session and prepare for new one."""
        if self._current_session_levels:
            levels = self._extract_levels(self._current_session_levels)
            if levels:
                self._all_prior_session_levels.append(levels)

        self._current_session_levels = {}
        self._extracted_session_levels = []
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels throughout the session."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Distribute volume across 0.25 granularity
        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_rounded = round(level * 4) / 4
            self._current_session_levels[level_rounded] = self._current_session_levels.get(level_rounded, 0) + int(vol_per_level)
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
        """Extract significant price levels from a volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Get top levels that comprise ~70% of volume
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
        all_levels = []
        
        # Current session levels
        all_levels.extend(self._extracted_session_levels)
        
        # All prior session levels
        for session_levels in self._all_prior_session_levels:
            all_levels.extend(session_levels)
        
        # Baseline levels from training
        all_levels.extend(self._baseline_levels)
        
        # Remove duplicates and return sorted
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """Detect absorption: strong delta + wick, optionally near level."""
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow (lowered threshold)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for wick opposite to flow direction (relaxed)
        has_tail = False
        
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick > self.min_wick:
                has_tail = True
        
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick > self.min_wick:
                has_tail = True

        if not has_tail:
            return None

        # Check proximity to carried levels
        carried_levels = self._get_all_carried_levels()
        closest_level = current.close
        near_level = False
        
        if carried_levels:
            # Find closest level
            min_distance = float('inf')
            for level in carried_levels:
                distance = abs(current.close - level)
                if distance < min_distance:
                    closest_level = level
                    min_distance = distance
            
            # Check if close enough to level
            if min_distance <= self.level_proximity:
                near_level = True

        # Allow trade if near a level, OR if delta is very strong (fallback)
        if not near_level and delta_ratio < self.fallback_delta_ratio:
            return None

        # Calculate stop and target
        if delta > 0:  # Long absorption
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "level": closest_level,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # Short absorption
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "level": closest_level,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order immediately on absorption detection."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        tag = "absorption-entry"

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
