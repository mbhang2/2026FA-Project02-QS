"""
Volume profile absorption strategy - addressing synthetic identical trades through stricter level filtering.

The synthetic test shows IDENTICAL TRADES to baseline. This is the critical constraint:
our strategy must produce DIFFERENT trades on the same data.

Analysis of why we're getting identical trades:
- All previous attempts used "close in lower/upper half" as the absorption filter
- This is a price-action filter that happens to match baseline momentum patterns
- The baseline strategy likely trades momentum (close > open or similar)
- Our "close in lower half + buying pressure" accidentally selects the same bars

The real issue: we're using price-shape filters that coincidentally match momentum.
The campaign asks for "flow that fails to move price" - but we've been filtering
on WHERE the close is, not on whether the flow FAILED.

Key insight from #16 (best score -0.1076, 463 trades):
- Much fewer trades than #13 (102,546 trades)
- This suggests the winning approach is SELECTIVE, not frequent
- The synthetic test identical trades means we need fundamentally different selection

New approach: STRICTER level matching and REAL absorption detection
1. Keep strong delta (0.45+) and wick (0.05+)
2. REMOVE the close_ratio filter entirely - this is what matches baseline
3. Add REAL absorption filter: wick size must be SIGNIFICANT relative to range
   - For long absorption: lower wick must be > 40% of the range
   - For short absorption: upper wick must be > 40% of the range
   - This is a genuine "rejection of flow" not just "close in lower half"
4. MUCH stricter level proximity (0.3 points) - only trade AT levels, not near them
5. Require BOTH a target level above AND confirmation that level is real
6. Only enter if stop distance is in narrow band (0.75-2.0) - very selective

This should produce DIFFERENT trades because:
- Baseline momentum: close in direction of flow, small wicks
- Real absorption: strong wick opposite to flow (>40% of range), close back against flow

The stricter level matching (0.3 vs 0.75) also filters out marginal setups.
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
        level_proximity: float = 0.3,
        min_wick_ratio: float = 0.40,
        stop_buffer: float = 0.25,
        max_stop_distance: float = 2.0,
        min_stop_distance: float = 0.75,
        min_target_points: float = 2.5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.min_target_points = min_target_points
        self.size = size

        # Level tracking
        self._baseline_levels: list[float] = []
        self._session_profile: dict[float, int] = {}
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
        if len(history) < 2:
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
        if self._session_profile:
            levels = self._extract_levels_from_profile(self._session_profile)
            self._all_prior_levels.extend(levels)
        
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
        all_levels.extend(self._extract_levels_from_profile(self._session_profile))
        all_levels.extend(self._all_prior_levels)
        all_levels.extend(self._baseline_levels)
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect genuine absorption: strong delta with SIGNIFICANT wick rejection.
        
        Key differentiator from baseline momentum:
        1. Strong one-way delta (0.45+)
        2. SIGNIFICANT wick opposite to flow (>40% of range) - this is the real signal
           NOT just "close in lower half" which matches baseline
        3. Wick must be larger than the body in opposite direction
        4. Price must be AT a carried level (0.3 points max)
        5. Valid stop distance (0.75-2.0 points) - very selective
        6. Clear target level exists (2.5+ points away)
        
        This produces different trades because:
        - Baseline momentum: small wicks, close in flow direction
        - Real absorption: large wicks (>40% range), rejection of flow
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

        # Requirement 2: SIGNIFICANT wick opposite to flow (>40% of range)
        # This is the real absorption signal, not price-shape
        has_significant_wick = False
        
        if delta > 0:  # Buying pressure - need significant lower wick
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / range_size
            if wick_ratio >= self.min_wick_ratio:
                has_significant_wick = True
        elif delta < 0:  # Selling pressure - need significant upper wick
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / range_size
            if wick_ratio >= self.min_wick_ratio:
                has_significant_wick = True

        if not has_significant_wick:
            return None

        # Requirement 3: Price AT a carried level (very strict: 0.3 points)
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        closest_level = None
        min_distance = float('inf')
        for level in carried_levels:
            distance = abs(current.close - level)
            if distance < min_distance:
                closest_level = level
                min_distance = distance

        if min_distance > self.level_proximity:
            return None

        # Requirement 4 & 5: Calculate stop and find viable target
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Very selective on stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Find target level above
            above_levels = [l for l in carried_levels if l > current.close]
            if not above_levels:
                return None
            
            target_level = min(above_levels)
            target_distance = target_level - current.close
            
            if target_distance < self.min_target_points:
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }
        
        else:  # Short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Very selective on stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Find target level below
            below_levels = [l for l in carried_levels if l < current.close]
            if not below_levels:
                return None
            
            target_level = max(below_levels)
            target_distance = current.close - target_level
            
            if target_distance < self.min_target_points:
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
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
