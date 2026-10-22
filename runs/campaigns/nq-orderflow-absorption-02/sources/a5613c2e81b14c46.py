"""
Volume profile absorption strategy - focus on identifying which levels actually
work by testing absorption at different price levels and measuring outcomes.

Analysis of campaign results:
- #16 scored -0.1076 with 463 trades (best real score)
- #74 scored -0.0980 with 2763 trades (second best, but higher volume)
- All strategies produce identical 70 synthetic trades
- The synthetic baseline suggests our detection logic works, but selectivity varies

Key insight from the pattern:
- #74 with 2763 trades beat #16 with 463 trades
- This means MORE trades at slightly better selectivity wins
- The problem is NOT trading too much, but trading the WRONG absorption signals

New approach - test absorption at MULTIPLE proximity ranges:
1. Keep core absorption detection tight (delta_ratio=0.35, wick requirement)
2. Test absorption signals at different distances from levels (0.5, 1.0, 2.0, 3.0)
3. Closer proximity = higher conviction = tighter stop
4. Farther proximity = lower conviction = wider stop
5. This creates a spectrum of trades with risk-adjusted sizing

Why this matters:
- Absorption at a level is strong (close proximity)
- Absorption NEAR a level might still be valid (medium proximity)
- The cost model favors fewer, better trades OR more, consistent trades
- By trading a spectrum, we get more coverage while maintaining selectivity

Parameters tuned for coverage:
- delta_ratio: 0.32 (slightly relaxed from #16's 0.35)
- Multiple proximity thresholds: [0.5, 1.0, 2.0, 3.0]
- Tighter stops for closer proximity (0.2) vs wider for farther (0.4)
- Moderate targets (2.5x) to maintain risk/reward
- Size=1 constant across all proximity bands

This addresses why #74 worked better: it had more trades. By creating a
proximity-based spectrum, we get more entry opportunities while keeping
each one disciplined by its own stop distance.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.32,
        min_wick_points: float = 0.0,
        target_multiple: float = 2.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.2,
        size: int = 1,
        vol_percentile: float = 0.70,
        proximity_thresholds: tuple = (0.5, 1.0, 2.0, 3.0),
        stop_buffers: tuple = (0.2, 0.25, 0.3, 0.4),
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.vol_percentile = vol_percentile
        self.proximity_thresholds = proximity_thresholds
        self.stop_buffers = stop_buffers

        # Level tracking
        self._baseline_levels: list[float] = []
        self._session_levels: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._all_prior_levels: list[float] = []
        
        # Track bars we've entered on to avoid re-entry
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

        # Detect absorption and test at multiple proximity levels
        absorptions = self._detect_absorption_spectrum(current)
        if absorptions:
            self._entered_bar_timestamps.add(current.close_ts)
            # Enter on the tightest proximity match (highest conviction)
            self._place_entry(ctx, absorptions[0])

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

        # Use 0.25-point granularity
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

        # Get top levels comprising vol_percentile of volume
        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * self.vol_percentile:
                break

        return sorted(list(set(top_levels)))

    def _get_all_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        all_levels.extend(self._all_prior_levels)
        all_levels.extend(self._baseline_levels)
        return sorted(list(set(all_levels)))

    def _detect_absorption_spectrum(self, current) -> list[dict]:
        """
        Detect absorption and return matches at multiple proximity levels.
        Returns list sorted by proximity (tightest first = highest conviction).
        """
        if current.volume == 0:
            return []

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return []

        # Require wick opposite to flow direction
        has_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return []

        # Get all levels
        all_levels = self._get_all_levels()
        if not all_levels:
            return []

        # Find closest level
        min_distance = min(abs(current.close - level) for level in all_levels)
        
        # Test against each proximity threshold
        valid_absorptions = []
        
        for idx, proximity in enumerate(self.proximity_thresholds):
            if min_distance <= proximity:
                stop_buffer = self.stop_buffers[idx]
                
                # Calculate stop and target
                if delta > 0:  # Long
                    stop_price = current.low - stop_buffer
                    stop_distance = current.close - stop_price
                    
                    if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                        continue
                    
                    valid_absorptions.append({
                        "direction": "long",
                        "stop_distance": stop_distance,
                        "target_distance": stop_distance * self.target_multiple,
                        "proximity": min_distance,
                        "proximity_band": idx,
                    })
                
                else:  # Short
                    stop_price = current.high + stop_buffer
                    stop_distance = stop_price - current.close
                    
                    if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                        continue
                    
                    valid_absorptions.append({
                        "direction": "short",
                        "stop_distance": stop_distance,
                        "target_distance": stop_distance * self.target_multiple,
                        "proximity": min_distance,
                        "proximity_band": idx,
                    })
        
        # Sort by proximity (tightest first = highest conviction)
        valid_absorptions.sort(key=lambda x: x["proximity"])
        
        return valid_absorptions

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
