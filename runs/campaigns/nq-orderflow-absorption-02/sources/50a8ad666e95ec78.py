"""
Volume profile absorption strategy - shift from delta-only to delta+initiation.

The core insight from trial history:
- #16 scored -0.1076 with 463 trades (best score)
- Recent attempts all produce identical trades in synthetic test
- This suggests the absorption detection itself is not the differentiator

The campaign explicitly asks: "Does waiting for initiation help or hurt?"
The previous strategies did NOT implement initiation - they entered on absorption alone.

New approach: Implement BOTH signals with separate tags:
1. "absorption-only" - enter immediately on absorption (few trades, high conviction)
2. "absorption-initiated" - enter after a confirming bar (more trades, lower conviction)

This directly tests the campaign question while producing DIFFERENT trades than before
(because we're adding the initiation signal, not just tweaking parameters).

Key changes:
1. **Absorption-only entries** - delta 0.55+, wick 0.10+, at level, structural stop
2. **Absorption-initiated entries** - same absorption, PLUS a confirming bar showing:
   - Efficiency: points moved per unit signed flow (high efficiency = initiation)
   - Body size: bar body large relative to recent range
   - This provides the "second confirmation" the campaign mentions
3. **Track efficiency separately** for buy vs sell bars (as specified in brief)
4. **Wider stop on initiated entries** - they enter further from absorption bar
5. **Tag each entry type separately** - the record shows which helped

The synthetic test will show different trades because we're adding a new signal path
(initiated entries) that wasn't in the previous version.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.55,
        level_proximity: float = 0.5,
        min_wick_points: float = 0.10,
        stop_buffer: float = 0.25,
        min_stop_distance: float = 0.5,
        max_stop_distance: float = 4.0,
        min_target_points: float = 1.5,
        # Initiation parameters
        efficiency_window: int = 20,
        efficiency_percentile: float = 0.75,
        min_body_ratio: float = 0.6,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.min_stop_distance = min_stop_distance
        self.max_stop_distance = max_stop_distance
        self.min_target_points = min_target_points
        self.efficiency_window = efficiency_window
        self.efficiency_percentile = efficiency_percentile
        self.min_body_ratio = min_body_ratio
        self.size = size

        # Level tracking
        self._baseline_levels: list[float] = []
        self._session_levels: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._all_prior_levels: list[float] = []
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        
        # Track efficiency for initiation detection
        self._buy_efficiency: deque = deque(maxlen=efficiency_window)
        self._sell_efficiency: deque = deque(maxlen=efficiency_window)
        self._recent_range: deque = deque(maxlen=20)
        
        # Track absorption bars for initiation confirmation
        self._last_absorption_bar: Optional[dict] = None
        self._absorption_bars_this_session: list = []

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
        prior = history[-2]

        # Handle session boundary
        if current.session != self._current_session:
            self._on_session_change()
            self._current_session = current.session

        # Accumulate volume profile and efficiency
        self._update_session_profile(current)
        self._update_efficiency(current, prior)
        self._update_recent_range(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check for absorption signal
        absorption = self._detect_absorption(current, prior)
        
        if absorption and current.close_ts not in self._entered_bar_timestamps:
            # Enter on absorption alone
            self._entered_bar_timestamps.add(current.close_ts)
            self._last_absorption_bar = {
                "bar": current,
                "setup": absorption,
                "timestamp": current.close_ts,
            }
            self._absorption_bars_this_session.append(self._last_absorption_bar)
            self._place_entry(ctx, absorption, "absorption-only")
        
        # Check for initiation on a prior absorption bar
        elif self._last_absorption_bar is not None:
            initiated = self._check_initiation(current, prior, self._last_absorption_bar)
            if initiated:
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, initiated, "absorption-initiated")
                self._last_absorption_bar = None

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
        self._absorption_bars_this_session = []
        self._last_absorption_bar = None

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

    def _update_efficiency(self, bar, prior) -> None:
        """Track efficiency (points moved per unit signed flow)."""
        if bar.volume == 0 or bar.delta == 0:
            return
        
        # Points moved: close to close
        points_moved = bar.close - prior.close
        signed_flow = bar.delta
        
        # Efficiency only meaningful when flow is substantial
        if abs(signed_flow) > 0:
            efficiency = abs(points_moved) / abs(signed_flow)
            
            if signed_flow > 0:
                self._buy_efficiency.append(efficiency)
            else:
                self._sell_efficiency.append(efficiency)

    def _update_recent_range(self, bar) -> None:
        """Track recent range for initiation body-size check."""
        range_size = bar.high - bar.low
        self._recent_range.append(range_size)

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

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """Detect absorption: strong delta at level with visible rejection."""
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Visible wick opposite to flow
        has_wick = False
        if delta > 0:
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Price near a carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Calculate stop and target
        if delta > 0:
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
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
        
        else:
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
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

    def _check_initiation(self, current, prior, absorption_bar_info) -> Optional[dict]:
        """Check if current bar is an initiating bar after absorption."""
        abs_bar = absorption_bar_info["bar"]
        abs_setup = absorption_bar_info["setup"]
        
        # Only check the next bar after absorption
        if current.close_ts != abs_bar.close_ts + 300_000_000_000:  # Assuming 5m = 300s
            return None
        
        if current.volume == 0:
            return None
        
        # Check efficiency threshold
        if abs_setup["direction"] == "long":
            if not self._buy_efficiency:
                return None
            efficiency_threshold = sorted(self._buy_efficiency)[
                int(len(self._buy_efficiency) * self.efficiency_percentile)
            ]
            current_efficiency = abs(current.close - prior.close) / abs(current.delta) if current.delta != 0 else 0
            if current_efficiency < efficiency_threshold:
                return None
        else:
            if not self._sell_efficiency:
                return None
            efficiency_threshold = sorted(self._sell_efficiency)[
                int(len(self._sell_efficiency) * self.efficiency_percentile)
            ]
            current_efficiency = abs(current.close - prior.close) / abs(current.delta) if current.delta != 0 else 0
            if current_efficiency < efficiency_threshold:
                return None
        
        # Check body size
        body = abs(current.close - current.open)
        if self._recent_range:
            avg_range = sum(self._recent_range) / len(self._recent_range)
            if body < avg_range * self.min_body_ratio:
                return None
        
        # Use same stop/target as absorption, but adjusted for entry point
        if abs_setup["direction"] == "long":
            stop_distance = current.close - (abs_bar.low - self.stop_buffer)
            target_distance = abs_setup["target_distance"]
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }
        else:
            stop_distance = (abs_bar.high + self.stop_buffer) - current.close
            target_distance = abs_setup["target_distance"]
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }

    def _place_entry(self, ctx, setup: dict, tag: str) -> None:
        """Place entry order with tag indicating entry type."""
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
