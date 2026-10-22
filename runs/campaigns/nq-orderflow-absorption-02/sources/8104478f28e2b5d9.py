"""
Volume profile absorption strategy - refined based on campaign evidence.

Campaign analysis reveals critical pattern:
- #81: -0.0096 with 622 trades (BEST: 10x better than #76)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

The synthetic test confirms core logic is sound (70 trades byte-identical).
The issue: parameters are reasonable but not aggressive enough.

Key insight from #81's exceptional performance:
1. Much stricter selectivity (622 vs 1079 trades = 43% fewer)
2. Better risk-adjusted returns (10x improvement)
3. Likely uses both directional asymmetry AND initiation filtering

This trial combines:
- #81's proven selectivity approach
- Directional asymmetry (different thresholds for long/short)
- Proper initiation filtering with efficiency-based confirmation
- Tighter level proximity and stop distances
- Higher delta_ratio thresholds

The hypothesis: #81 succeeds by:
1. Being much more selective about WHICH absorption bars to trade
2. Only trading certain directions under certain conditions
3. Waiting for initiation confirmation that shows real conviction
4. Using tighter stops and more conservative targets

Expected: Score near #81's -0.0096 with 550-700 trades on real data.
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        # Directional asymmetry: shorts require higher delta_ratio
        absorption_delta_ratio_long: float = 0.32,
        absorption_delta_ratio_short: float = 0.38,
        level_proximity: float = 1.2,
        min_wick_points: float = 0.60,
        stop_buffer: float = 0.25,
        target_multiple_long: float = 2.2,
        target_multiple_short: float = 2.4,
        max_stop_distance: float = 2.5,
        min_stop_distance: float = 0.25,
        size: int = 1,
        top_levels_per_session: int = 3,
        # Initiation filtering parameters
        use_initiation: bool = True,
        efficiency_threshold: float = 1.3,
        efficiency_lookback: int = 15,
        min_body_ratio: float = 0.40,
        min_tick_count: int = 4,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio_long = absorption_delta_ratio_long
        self.absorption_delta_ratio_short = absorption_delta_ratio_short
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple_long = target_multiple_long
        self.target_multiple_short = target_multiple_short
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.top_levels_per_session = top_levels_per_session
        self.use_initiation = use_initiation
        self.efficiency_threshold = efficiency_threshold
        self.efficiency_lookback = efficiency_lookback
        self.min_body_ratio = min_body_ratio
        self.min_tick_count = min_tick_count

        # Level tracking
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        
        # Absorption candidate tracking
        self._absorption_candidate: Optional[dict] = None
        self._absorption_bar_ts: Optional[int] = None
        
        # Efficiency tracking for initiation filtering
        self._efficiency_history: deque = deque(maxlen=efficiency_lookback)
        self._range_history: deque = deque(maxlen=efficiency_lookback)
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

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
            self._on_session_change(current.session)
            self._current_session = current.session

        # Accumulate volume profile
        self._update_session_profile(current)
        
        # Track efficiency metrics for initiation
        self._update_efficiency_metrics(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Check if we should enter on initiation of previous absorption
        if self.use_initiation and self._absorption_candidate is not None:
            if self._should_initiate(current):
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, self._absorption_candidate, tag="initiated")
                self._absorption_candidate = None
                self._absorption_bar_ts = None
                return

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            if self.use_initiation:
                # Store as candidate, wait for initiation
                self._absorption_candidate = setup
                self._absorption_bar_ts = current.close_ts
            else:
                # Trade absorption directly
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, setup, tag="absorption")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and carry forward."""
        if self._session_levels:
            levels = self._extract_levels_from_profile(self._session_levels)
            self._prior_session_levels.append(levels)
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_bar_timestamps = set()
        self._absorption_candidate = None
        self._absorption_bar_ts = None
        self._efficiency_history.clear()
        self._range_history.clear()

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

    def _update_efficiency_metrics(self, bar) -> None:
        """Track bar efficiency for initiation filtering."""
        if bar.volume <= 0:
            return
        
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return
        
        # Efficiency: range traveled per unit of absolute delta
        # Lower efficiency = better (price moved with less one-sided flow)
        abs_delta = abs(bar.delta)
        if abs_delta > 0:
            efficiency = bar_range / abs_delta
            self._efficiency_history.append(efficiency)
        
        self._range_history.append(bar_range)

    def _extract_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract the most significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        top_levels = [level for level, vol in sorted_items[:self.top_levels_per_session]]

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        return sorted(list(set(all_levels)))

    def _should_initiate(self, current) -> bool:
        """
        Check if current bar shows initiation characteristics.
        
        Initiation requires:
        1. Efficient price movement (low points/delta ratio)
        2. Good body size relative to range
        3. Directional conviction matching absorption
        4. Sufficient tick activity
        """
        if not self._efficiency_history or self._absorption_candidate is None:
            return False
        
        # Require minimum tick activity
        if current.tick_count < self.min_tick_count:
            return False
        
        # Check bar body size relative to range
        bar_range = current.high - current.low
        if bar_range <= 0:
            return False
        
        body = abs(current.close - current.open)
        body_ratio = body / bar_range
        if body_ratio < self.min_body_ratio:
            return False
        
        # Check efficiency
        if current.volume <= 0:
            return False
        
        abs_delta = abs(current.delta)
        if abs_delta == 0:
            return False
        
        current_efficiency = bar_range / abs_delta
        
        # Calculate average efficiency threshold
        avg_efficiency = sum(self._efficiency_history) / len(self._efficiency_history)
        
        # Initiation shows BETTER efficiency (lower points/delta)
        # So we want current_efficiency < threshold
        threshold = avg_efficiency / self.efficiency_threshold
        
        if current_efficiency >= threshold:
            return False
        
        # Check direction matches absorption candidate
        candidate_direction = self._absorption_candidate.get("direction")
        if candidate_direction == "long" and current.delta <= 0:
            return False
        if candidate_direction == "short" and current.delta >= 0:
            return False
        
        return True

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried volume levels.
        Uses directional asymmetry: shorts require higher delta_ratio.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Directional thresholds
        if delta > 0:  # Long
            if delta_ratio < self.absorption_delta_ratio_long:
                return None
        elif delta < 0:  # Short
            if delta_ratio < self.absorption_delta_ratio_short:
                return None
        else:
            return None

        # Require wick showing price rejection
        has_wick = False
        if delta > 0:  # Buying pressure
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure
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
                "target_distance": stop_distance * self.target_multiple_long,
            }
        
        else:  # Short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple_short,
            }

    def _place_entry(self, ctx, setup: dict, tag: str = "absorption") -> None:
        """Place entry order on absorption or initiation detection."""
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
