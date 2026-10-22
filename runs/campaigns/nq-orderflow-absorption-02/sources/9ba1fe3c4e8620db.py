"""
Volume profile absorption strategy - diagnostic run to understand synthetic baseline behavior.

Analysis of the campaign:
- #16 scored -0.1076 with 463 trades (only real positive score)
- All recent trials produce identical synthetic trades (70 trades)
- The consistent 70 synthetic trades across vastly different parameters is a signal

Critical observation: The synthetic baseline produces exactly 70 trades regardless of:
- absorption_delta_ratio (0.35 to 0.50)
- level_proximity (2.0 to 4.0)
- target_multiple (2.0 to 3.5)
- level percentile (50% to 70%)
- granularity (0.25 to 0.5)
- level accumulation strategies

This suggests the synthetic baseline has a fallback behavior that fires when absorption
detection is too strict OR when it's too permissive - a middle ground that always
produces ~70 trades.

The key insight: #16 produced 463 trades on real data, meaning absorption detection
was firing frequently. But it scored -0.1076, meaning those trades lost money.

New hypothesis: The issue is NOT the detection logic, but the ENTRY TIMING.
- Absorption detection identifies where flow failed to move price
- But entering immediately on absorption bar may be too early
- The campaign brief mentions "confirming bar" - a bar showing efficient movement

This trial implements the campaign's optional waiting mechanism:
1. Detect absorption exactly like #16 (delta_ratio=0.35, proximity=4.0)
2. But DON'T enter immediately
3. Wait for a confirming bar: one that moves price efficiently with strong flow
4. Only enter after confirmation
5. Tag entries as "absorption_immediate" vs "absorption_confirmed" to measure impact

The reasoning: If absorption is real, confirmed entries should outperform immediate ones.
If the campaign was right about initiation, this should improve on #16's -0.1076.

This addresses why synthetic shows 70 trades: the synthetic baseline may be entering
on absorption immediately (no confirmation), while real data benefits from waiting.
By implementing both signals tagged separately, we can measure which works.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        level_proximity: float = 4.0,
        min_wick_points: float = 0.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        require_confirmation: bool = True,
        efficiency_lookback: int = 10,
        efficiency_threshold: float = 1.5,
        min_body_ratio: float = 0.6,
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
        self.require_confirmation = require_confirmation
        self.efficiency_lookback = efficiency_lookback
        self.efficiency_threshold = efficiency_threshold
        self.min_body_ratio = min_body_ratio

        # Level tracking
        self._baseline_levels: list[float] = []
        self._session_levels: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._all_prior_levels: list[float] = []
        
        # Absorption tracking - map of bar timestamp to absorption setup
        self._pending_absorptions: dict[int, dict] = {}
        
        # Efficiency tracking for confirmation
        self._bar_efficiencies: list[float] = []
        self._bar_ranges: list[float] = []

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

        # Update efficiency tracking
        self._update_efficiency_tracking(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check for confirmation on pending absorption setups
        if self._pending_absorptions:
            self._check_confirmations(ctx, current, history)

        # Detect new absorption signals
        absorption = self._detect_absorption(current)
        if absorption:
            if self.require_confirmation:
                # Store for later confirmation
                self._pending_absorptions[current.close_ts] = absorption
            else:
                # Enter immediately
                self._place_entry(ctx, absorption, "absorption_immediate")

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
        self._pending_absorptions = {}
        self._bar_efficiencies = []
        self._bar_ranges = []

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
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _update_efficiency_tracking(self, bar) -> None:
        """Track bar efficiency for confirmation detection."""
        if bar.volume <= 0:
            return
        
        # Efficiency: distance traveled per unit of signed flow
        signed_flow = abs(bar.delta)
        if signed_flow > 0:
            distance = bar.high - bar.low
            efficiency = distance / (signed_flow / 1000.0)  # Normalize
            self._bar_efficiencies.append(efficiency)
        
        # Track range
        bar_range = bar.high - bar.low
        self._bar_ranges.append(bar_range)
        
        # Keep only recent history
        if len(self._bar_efficiencies) > self.efficiency_lookback:
            self._bar_efficiencies.pop(0)
        if len(self._bar_ranges) > self.efficiency_lookback:
            self._bar_ranges.pop(0)

    def _check_confirmations(self, ctx, current, history) -> None:
        """Check if pending absorption setups have confirming bars."""
        # Remove old pending setups (older than 5 bars)
        cutoff_ts = current.close_ts - (5 * 300_000_000_000)  # 5 * 5m in nanoseconds
        expired = [ts for ts in self._pending_absorptions if ts < cutoff_ts]
        for ts in expired:
            del self._pending_absorptions[ts]
        
        # Check current bar for confirmation
        if not self._pending_absorptions:
            return
        
        # Get the oldest pending absorption
        oldest_ts = min(self._pending_absorptions.keys())
        absorption = self._pending_absorptions[oldest_ts]
        
        # Check if current bar confirms it
        if self._is_confirming_bar(current, absorption):
            del self._pending_absorptions[oldest_ts]
            self._place_entry(ctx, absorption, "absorption_confirmed")

    def _is_confirming_bar(self, bar, absorption) -> bool:
        """Check if a bar meets confirmation criteria."""
        if bar.volume == 0:
            return False
        
        # Need strong directional flow matching absorption direction
        direction = absorption["direction"]
        delta = bar.delta
        delta_ratio = abs(delta) / bar.volume
        
        # Weaker threshold for confirmation (0.25 vs 0.35 for initial detection)
        if delta_ratio < 0.25:
            return False
        
        # Flow must match direction
        if direction == "long" and delta <= 0:
            return False
        if direction == "short" and delta >= 0:
            return False
        
        # Check efficiency: bar should move efficiently
        if self._bar_efficiencies:
            avg_efficiency = sum(self._bar_efficiencies) / len(self._bar_efficiencies)
            bar_efficiency = (bar.high - bar.low) / (abs(bar.delta) / 1000.0 + 0.01)
            if bar_efficiency < avg_efficiency * self.efficiency_threshold:
                return False
        
        # Check body size: body should be large relative to recent range
        if self._bar_ranges:
            avg_range = sum(self._bar_ranges) / len(self._bar_ranges)
            body = abs(bar.close - bar.open)
            if body < avg_range * self.min_body_ratio:
                return False
        
        return True

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
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        all_levels.extend(self._all_prior_levels)
        all_levels.extend(self._baseline_levels)
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong delta at a carried level with wick opposite to flow.
        Uses #16's exact parameters that scored -0.1076 with 463 trades.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for wick opposite to flow direction
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
            return None

        # Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
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

    def _place_entry(self, ctx, setup: dict, tag: str) -> None:
        """Place entry order on absorption detection."""
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
