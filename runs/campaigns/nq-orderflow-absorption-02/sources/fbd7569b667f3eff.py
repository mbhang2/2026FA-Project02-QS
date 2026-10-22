"""
Volume profile absorption strategy - simplify to core absorption signal without confirmation complexity.

The synthetic identical trades problem persists because the multi-bar confirmation logic
is still fundamentally matching baseline momentum selection. The issue is deeper:
we're trying to detect "absorption" with filters that happen to align with momentum patterns.

Key realization from campaign brief:
- "Flow that fails to move price" is the core signal
- This should be RARE and STRUCTURAL, not frequent
- The 50% floor exists because single-session trades won't survive costs
- Multi-session holding is expected for moves between levels

The synthetic test (70 trades identical to baseline) suggests our level-based filtering
isn't creating a different signal - it's just selecting the same bars differently.

New approach - focus on the RAREST, most STRUCTURAL absorption:
1. Only trade at MAJOR levels (top 20% by volume, not 70%)
2. Require EXTREME absorption: delta_ratio 0.65+ (not 0.45-0.50)
3. Wick must be VERY SIGNIFICANT: 0.35+ points absolute, AND >50% of range
4. Price must be EXACTLY at level: 0.15 point tolerance (not 0.75)
5. Only long-term targets: 4.0+ points to next level (forces multi-session holds)
6. Very tight stop band: 0.5-1.5 points only (rejects marginal setups)

This creates a fundamentally different selection because:
- We're only trading the absolute strongest absorption at major levels
- The filters are so strict that very few bars qualify
- The synthetic test should show MUCH fewer trades if filters work
- If still identical, the synthetic baseline IS absorption-based (unlikely)

The key difference from all prior attempts: we're not trying to catch "good" absorption,
we're trying to catch EXTREME absorption that is so obvious it should work.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.65,
        level_proximity: float = 0.15,
        min_wick_points: float = 0.35,
        min_wick_ratio: float = 0.50,
        stop_buffer: float = 0.25,
        max_stop_distance: float = 1.5,
        min_stop_distance: float = 0.5,
        min_target_points: float = 4.0,
        size: int = 1,
        major_level_percentile: float = 0.20,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.min_target_points = min_target_points
        self.size = size
        self.major_level_percentile = major_level_percentile

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
                self._baseline_levels = self._extract_major_levels(train_bars)
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

        # Detect EXTREME absorption at MAJOR levels only
        prior = history[-2]
        absorption = self._detect_extreme_absorption(current, prior)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract major levels from completed session."""
        if self._session_profile:
            major_levels = self._extract_major_levels_from_profile(self._session_profile)
            self._all_prior_levels.extend(major_levels)
            self._all_prior_levels = sorted(list(set(self._all_prior_levels)))
        
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

    def _extract_major_levels(self, bars) -> list[float]:
        """Extract MAJOR levels from a sequence of bars."""
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
        
        return self._extract_major_levels_from_profile(profile)

    def _extract_major_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract only MAJOR levels (top 20% by volume)."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Get only top levels comprising 20% of volume (very selective)
        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * self.major_level_percentile:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_major_levels(self) -> list[float]:
        """Get all carried MAJOR levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_major_levels_from_profile(self._session_profile))
        all_levels.extend(self._all_prior_levels)
        all_levels.extend(self._baseline_levels)
        return sorted(list(set(all_levels)))

    def _detect_extreme_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect EXTREME absorption at MAJOR levels.
        
        This is the most selective filter possible:
        1. EXTREME delta: 0.65+ (not 0.50)
        2. MAJOR level only: top 20% by volume, not 70%
        3. EXACTLY at level: 0.15 point tolerance, not 0.75
        4. VERY SIGNIFICANT wick: 0.35+ points AND >50% of range
        5. Tight stop band: 0.5-1.5 points (rejects marginal setups)
        6. Long-term target: 4.0+ points (forces multi-session holds)
        
        If this still produces identical trades on synthetic data, the baseline
        IS absorption-based or the synthetic data has no real absorption.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Requirement 1: EXTREME one-sided flow (0.65+)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low

        # Requirement 2 & 3: MAJOR level only, with EXACT proximity (0.15)
        carried_major_levels = self._get_carried_major_levels()
        if not carried_major_levels:
            return None

        closest_level = None
        min_distance = float('inf')
        for level in carried_major_levels:
            distance = abs(current.close - level)
            if distance < min_distance:
                closest_level = level
                min_distance = distance

        if min_distance > self.level_proximity:
            return None

        # Requirement 4: VERY SIGNIFICANT wick (0.35+ points AND >50% of range)
        has_extreme_wick = False
        
        if delta > 0:  # Buying pressure - need significant lower wick
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / range_size if range_size > 0 else 0
            if lower_wick >= self.min_wick_points and wick_ratio >= self.min_wick_ratio:
                has_extreme_wick = True
        elif delta < 0:  # Selling pressure - need significant upper wick
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / range_size if range_size > 0 else 0
            if upper_wick >= self.min_wick_points and wick_ratio >= self.min_wick_ratio:
                has_extreme_wick = True

        if not has_extreme_wick:
            return None

        # Requirement 5 & 6: Calculate stop and find viable long-term target
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Tight stop band (0.5-1.5)
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Find target level far above (4.0+ points)
            above_levels = [l for l in carried_major_levels if l > current.close]
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
            
            # Tight stop band (0.5-1.5)
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            # Find target level far below (4.0+ points)
            below_levels = [l for l in carried_major_levels if l < current.close]
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
        """Place entry order on extreme absorption detection."""
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
