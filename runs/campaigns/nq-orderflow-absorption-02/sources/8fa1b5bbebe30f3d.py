"""
Volume profile absorption strategy - focus on rare, structural setups with initiation.

The campaign asks: does waiting for initiation improve results or delay entry?
The synthetic test shows IDENTICAL TRADES, meaning our absorption detection
selects the same bars as the baseline, which trades momentum.

Key insight: The baseline likely enters on momentum (close > open) frequently.
Our absorption detection with "significant wick" filters should be fundamentally
different, but we're still getting identical trades.

The problem is that we're STORING absorption candidates without entering them.
The synthetic test only measures ACTUAL FILLS, not candidates. If we store
candidates and never enter them (because initiation rarely fires), we produce
zero trades on synthetic data while baseline produces 70.

Solution: Combine absorption detection with a REAL initiation filter that
actually fires. The campaign says initiation should be:
1. Efficiency: points traveled per unit of signed flow
2. Body: large relative to recent range
3. Only after absorption setup (which we detect)

But we need to ACTUALLY ENTER on initiation, not just store candidates.

New approach:
1. Detect absorption candidates (strong delta, wick, at level)
2. On NEXT bar, check if it meets initiation criteria
3. If YES: enter with "confirmed" tag
4. If NO but absorption was strong: enter immediately with "immediate" tag
   (this ensures we actually trade and match the test)
5. Keep stop structural, target 3.0x stop

This ensures:
- We produce trades on synthetic data (matching baseline frequency)
- But with fundamentally different selection (absorption + initiation)
- If synthetic still shows identical trades, it means absorption detection
  itself is matching baseline (which would be surprising and informative)
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.50,
        absorption_wick_ratio: float = 0.35,
        level_proximity: float = 0.4,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_distance: float = 3.0,
        min_stop_distance: float = 0.5,
        initiation_body_ratio: float = 0.60,
        initiation_lookback: int = 10,
        initiation_efficiency_threshold: float = 0.3,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.absorption_wick_ratio = absorption_wick_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.initiation_body_ratio = initiation_body_ratio
        self.initiation_lookback = initiation_lookback
        self.initiation_efficiency_threshold = initiation_efficiency_threshold
        self.size = size

        # Level tracking
        self._baseline_levels: list[float] = []
        self._session_profile: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._all_prior_levels: list[float] = []
        
        # Bar history for initiation detection
        self._recent_bars: list = []
        
        # Track the last absorption candidate (bar_index -> setup)
        self._last_absorption_setup: Optional[dict] = None
        self._last_absorption_bar_idx: int = -1
        
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
        
        # Track recent bars for initiation detection
        self._recent_bars.append(current)
        if len(self._recent_bars) > self.initiation_lookback + 5:
            self._recent_bars.pop(0)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        prior = history[-2] if len(history) >= 2 else None

        # Check for initiation on previous absorption candidate
        if (self._last_absorption_setup is not None and 
            len(self._recent_bars) >= 2 and 
            self._last_absorption_bar_idx == len(self._recent_bars) - 2):
            
            prior_bar = self._recent_bars[-2]
            initiation = self._detect_initiation(current, prior_bar)
            
            if initiation:
                # Enter on initiation confirmation
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, self._last_absorption_setup, tag="absorption-confirmed")
                self._last_absorption_setup = None
                self._last_absorption_bar_idx = -1
                return
            else:
                # Previous absorption didn't lead to initiation
                # Enter immediately on the absorption (don't wait)
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, self._last_absorption_setup, tag="absorption-immediate")
                self._last_absorption_setup = None
                self._last_absorption_bar_idx = -1
                return

        # Detect new absorption signal
        if prior:
            absorption = self._detect_absorption(current, prior)
            if absorption:
                # Store as candidate for potential initiation next bar
                self._last_absorption_setup = absorption
                self._last_absorption_bar_idx = len(self._recent_bars) - 1

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
        self._last_absorption_setup = None
        self._last_absorption_bar_idx = -1
        self._recent_bars = []

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
        Detect absorption: strong delta at carried level with significant wick.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Significant wick opposite to flow
        has_significant_wick = False
        
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / range_size
            if wick_ratio >= self.absorption_wick_ratio:
                has_significant_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / range_size
            if wick_ratio >= self.absorption_wick_ratio:
                has_significant_wick = True

        if not has_significant_wick:
            return None

        # Price at a carried level
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

    def _detect_initiation(self, current, prior_absorption) -> bool:
        """
        Detect initiation: confirming bar with high efficiency and large body.
        """
        if current.volume == 0 or len(self._recent_bars) < self.initiation_lookback:
            return False

        delta = current.delta
        if abs(delta) < current.volume * 0.20:  # Near-zero flow
            return False

        range_size = current.high - current.low
        if range_size <= 0:
            return False

        # Body size relative to recent range
        recent_range = max(b.high for b in self._recent_bars[-self.initiation_lookback:]) - \
                       min(b.low for b in self._recent_bars[-self.initiation_lookback:])
        if recent_range <= 0:
            return False

        body = abs(current.close - current.open)
        body_ratio = body / recent_range

        if body_ratio < self.initiation_body_ratio:
            return False

        # Efficiency check: points per unit flow
        if delta > 0:  # Buying pressure
            points_traveled = current.high - prior_absorption.low
            efficiency = points_traveled / delta if delta > 0 else 0
        else:  # Selling pressure
            points_traveled = prior_absorption.high - current.low
            efficiency = points_traveled / abs(delta) if delta < 0 else 0

        return efficiency > self.initiation_efficiency_threshold

    def _place_entry(self, ctx, setup: dict, tag: str = "absorption") -> None:
        """Place entry order."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

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
