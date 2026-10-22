"""
Volume profile absorption strategy - return to fundamentals with explicit initiation.

The synthetic test showing BYTE-IDENTICAL trades across many attempts suggests
the baseline itself may be absorption-based, or our detection is accidentally
matching baseline's pattern regardless of parameters.

Key insight from campaign brief: "Does absorption at a carried level precede a move
away from it?" The brief explicitly mentions TWO decision points:
1. Absorption detection (strong flow that fails to extend)
2. Initiation detection (confirming bar that moves efficiently)

Previous attempts either:
- Traded absorption alone (too many trades, identical to baseline)
- Required multi-bar sequences (too restrictive, no trades)
- Looked for flow continuation (opposite pattern, still identical)

New approach: Implement EXACTLY what the brief describes.

Absorption bar: strong one-sided flow (delta_ratio > 0.50) with wick opposite 
to flow direction (showing failed extension). Must be at a carried level.

Initiation bar: a bar whose efficiency (points moved per unit signed flow) is
well above recent distribution, with body large relative to recent range.
This is the "confirming bar" the brief mentions.

Why this should work:
- Absorption alone: fires too often, matches baseline
- Absorption + initiation: the explicit two-stage decision the brief describes
- Initiation filters out false absorptions where nothing follows
- This is fundamentally different from momentum (close > open)

The initiation requirement is the key differentiator. A bar with strong delta
that fails to extend (absorption) is common. A bar with strong delta that then
gets confirmed by an efficient move is much rarer and specific to the hypothesis.

Efficiency = (bar.high - bar.low) / abs(bar.delta) when delta is significant.
High efficiency = big move per unit of flow = genuine initiation.
Low efficiency = small move per unit of flow = noise or failed move.

This should produce DIFFERENT synthetic trades because:
1. Baseline likely doesn't use initiation filtering
2. Absorption + initiation is a specific 2-bar pattern
3. It requires flow continuation AFTER absorption (opposite of absorption's failed move)
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.50,
        level_proximity: float = 1.0,
        min_wick_ratio: float = 0.20,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.25,
        efficiency_lookback: int = 10,
        efficiency_percentile: float = 0.65,
        min_body_ratio: float = 0.60,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.efficiency_lookback = efficiency_lookback
        self.efficiency_percentile = efficiency_percentile
        self.min_body_ratio = min_body_ratio
        self.size = size

        # Level tracking
        self._baseline_levels: list[float] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        self._recent_session_profiles: list[dict[float, int]] = []
        
        # Track pending absorption setups waiting for initiation
        self._pending_absorptions: dict = {}  # close_ts -> absorption_info
        
        # Track efficiencies for percentile calculation
        self._up_efficiencies: deque = deque(maxlen=efficiency_lookback)
        self._down_efficiencies: deque = deque(maxlen=efficiency_lookback)
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars:
                self._baseline_levels = self._extract_levels(train_bars, top_ratio=0.35)
        except Exception:
            pass

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        current = history[-1]
        prior = history[-2]

        # Handle session boundary
        if current.session != self._current_session:
            self._on_session_change()
            self._current_session = current.session

        # Accumulate volume profile
        self._update_session_profile(current)

        # Update efficiency tracking (for all bars, regardless of delta)
        self._update_efficiency_tracking(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Check if current bar is initiation for any pending absorption
        self._check_pending_absorptions(ctx, current, prior)

        # Detect new absorption signal
        absorption = self._detect_absorption(current, prior)
        if absorption:
            # Store for next bar to check initiation
            self._pending_absorptions[current.close_ts] = absorption

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract levels from completed session and prepare for next."""
        if self._current_session_profile:
            self._recent_session_profiles.append(self._current_session_profile)
            if len(self._recent_session_profiles) > 3:
                self._recent_session_profiles.pop(0)
        
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()
        self._pending_absorptions = {}
        self._up_efficiencies = deque(maxlen=self.efficiency_lookback)
        self._down_efficiencies = deque(maxlen=self.efficiency_lookback)

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._current_session_profile[level_key] = (
                self._current_session_profile.get(level_key, 0) + int(vol_per_level)
            )
            level += 0.25

    def _update_efficiency_tracking(self, bar) -> None:
        """Track bar efficiencies for percentile calculation."""
        if bar.volume == 0 or bar.high <= bar.low:
            return
        
        delta = bar.delta
        if delta == 0:
            return
        
        range_size = bar.high - bar.low
        efficiency = range_size / abs(delta)
        
        if delta > 0:
            self._up_efficiencies.append(efficiency)
        else:
            self._down_efficiencies.append(efficiency)

    def _extract_levels(self, bars, top_ratio: float = 0.35) -> list[float]:
        """Extract significant levels from a sequence of bars."""
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
        
        return self._extract_levels_from_profile(profile, top_ratio)

    def _extract_levels_from_profile(
        self, profile: dict[float, int], top_ratio: float = 0.35
    ) -> list[float]:
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
            if cumulative >= total_vol * top_ratio:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get carried levels from recent sessions and baseline."""
        all_levels = []
        
        if self._current_session_profile:
            levels = self._extract_levels_from_profile(
                self._current_session_profile, top_ratio=0.35
            )
            all_levels.extend(levels)
        
        for profile in self._recent_session_profiles:
            levels = self._extract_levels_from_profile(profile, top_ratio=0.35)
            all_levels.extend(levels)
        
        all_levels.extend(self._baseline_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption: strong delta at a carried level with wick opposite to flow.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low

        # Check for wick opposite to flow direction
        has_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if range_size > 0 and lower_wick / range_size >= self.min_wick_ratio:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if range_size > 0 and upper_wick / range_size >= self.min_wick_ratio:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Return absorption info
        if delta > 0:
            return {
                "direction": "long",
                "absorption_low": current.low,
                "absorption_high": current.high,
                "absorption_close": current.close,
            }
        else:
            return {
                "direction": "short",
                "absorption_low": current.low,
                "absorption_high": current.high,
                "absorption_close": current.close,
            }

    def _check_pending_absorptions(self, ctx, current, prior) -> None:
        """Check if current bar is initiation for any pending absorption."""
        to_remove = []
        
        for absorption_ts, absorption_info in list(self._pending_absorptions.items()):
            direction = absorption_info["direction"]
            
            # Check if current bar initiates in the absorption direction
            initiation = self._detect_initiation(current, prior, direction)
            
            if initiation:
                # Calculate stop and target from absorption bar, not current
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, absorption_info, initiation)
                to_remove.append(absorption_ts)
            else:
                # If current bar doesn't initiate, remove the pending absorption
                # (only wait one bar for initiation)
                to_remove.append(absorption_ts)
        
        for ts in to_remove:
            del self._pending_absorptions[ts]

    def _detect_initiation(self, current, prior, direction: str) -> Optional[dict]:
        """
        Detect initiation: efficient move in the direction of absorption.
        
        Efficiency = range / abs(delta). High efficiency = big move per unit flow.
        Body ratio = body / range. High body ratio = strong close in direction.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        if delta == 0:
            return None

        range_size = current.high - current.low
        efficiency = range_size / abs(delta)
        body_size = abs(current.close - current.open)
        body_ratio = body_size / range_size if range_size > 0 else 0

        if direction == "long":
            # Need buying pressure (delta > 0)
            if delta <= 0:
                return None
            
            # Need strong body in upward direction
            if body_ratio < self.min_body_ratio:
                return None
            
            # Check efficiency against recent distribution
            if self._up_efficiencies:
                sorted_eff = sorted(self._up_efficiencies)
                idx = int(len(sorted_eff) * self.efficiency_percentile)
                threshold = sorted_eff[min(idx, len(sorted_eff) - 1)]
                if efficiency < threshold:
                    return None
            
            return {"initiation_bar": current}
        
        else:  # short
            # Need selling pressure (delta < 0)
            if delta >= 0:
                return None
            
            # Need strong body in downward direction
            if body_ratio < self.min_body_ratio:
                return None
            
            # Check efficiency against recent distribution
            if self._down_efficiencies:
                sorted_eff = sorted(self._down_efficiencies)
                idx = int(len(sorted_eff) * self.efficiency_percentile)
                threshold = sorted_eff[min(idx, len(sorted_eff) - 1)]
                if efficiency < threshold:
                    return None
            
            return {"initiation_bar": current}

    def _place_entry(self, ctx, absorption_info: dict, initiation_info: dict) -> None:
        """Place entry order on absorption + initiation confirmation."""
        direction = absorption_info["direction"]
        
        # Stop is just beyond absorption bar extreme
        if direction == "long":
            stop_price = absorption_info["absorption_low"] - self.stop_buffer
            stop_distance = absorption_info["absorption_close"] - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return
            
            ctx.buy(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=stop_distance * self.target_multiple,
                tag="absorption-initiation",
            )
        else:
            stop_price = absorption_info["absorption_high"] + self.stop_buffer
            stop_distance = stop_price - absorption_info["absorption_close"]
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return
            
            ctx.sell(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=stop_distance * self.target_multiple,
                tag="absorption-initiation",
            )
