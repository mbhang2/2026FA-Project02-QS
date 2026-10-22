"""
Volume profile absorption strategy - targeting #81's -0.0096 by adding
directional bias filtering and improving level selection.

Analysis of top performers:
- #81: -0.0096 with 622 trades (BEST OVERALL)
- #76: -0.0893 with 1079 trades
- #74: -0.0980 with 2763 trades

The byte-identical synthetic results on previous attempts indicate that the
synthetic data doesn't have bars matching certain structural criteria. However,
#81 achieved excellent real-world performance with 622 trades.

Key insight: #81 succeeded with:
1. Moderate trade frequency (622 trades)
2. Strong directional filtering (delta_ratio=0.28)
3. Tight level proximity (3.5 points)
4. Conservative target multiple (4.0x)

New approach to beat #81:
1. Add session-level directional bias: only take longs in sessions with net
   upward bias, and shorts in sessions with net downward bias. This filters
   for trades aligned with session structure.
2. Improve level selection: use top 5-6 most significant levels per session
   instead of 70th percentile, focusing on the most robust price nodes.
3. Add multi-bar confirmation: require that the absorption bar is preceded
   by at least one bar in the same directional direction, confirming momentum.
4. Tighten wick requirement slightly: min_wick_points=0.25 (was 0.20)
5. Keep #81's proven core: delta_ratio=0.28, proximity=3.5, target_multiple=4.0

Expected: 600-800 trades with score better than #81's -0.0096 by filtering
for higher-quality setups that align with session bias and multi-bar structure.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 3.5,
        min_body_ratio: float = 0.45,
        min_tick_count: int = 7,
        min_bar_range: float = 1.5,
        max_bar_range: float = 8.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_percentile: float = 0.70,
        volume_ratio_threshold: float = 0.75,
        min_wick_points: float = 0.25,
        session_bias_window: int = 20,
        top_levels_count: int = 6,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_body_ratio = min_body_ratio
        self.min_tick_count = min_tick_count
        self.min_bar_range = min_bar_range
        self.max_bar_range = max_bar_range
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_percentile = level_percentile
        self.volume_ratio_threshold = volume_ratio_threshold
        self.min_wick_points = min_wick_points
        self.session_bias_window = session_bias_window
        self.top_levels_count = top_levels_count

        # Level tracking
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
        # Track session volumes for context
        self._session_volumes: list[int] = []
        
        # Track session deltas for directional bias
        self._session_deltas: list[int] = []
        
        # Track recent bars for momentum confirmation
        self._recent_bars: list = []
        
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
            self._session_volumes = []
            self._session_deltas = []
            self._recent_bars = []

        # Accumulate volume profile and track volumes/deltas
        self._update_session_profile(current)
        self._session_volumes.append(current.volume)
        self._session_deltas.append(current.delta)
        self._recent_bars.append(current)
        
        # Keep only recent bars for momentum check
        if len(self._recent_bars) > 3:
            self._recent_bars.pop(0)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, setup)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Extract levels from completed session and carry forward."""
        if self._session_levels:
            self._prior_session_levels.append(dict(self._session_levels))
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        self._session_levels = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels with 0.25-point granularity."""
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

    def _extract_top_levels(self, profile: dict[float, int], count: int) -> list[float]:
        """Extract the top N most significant levels by volume."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        top_levels = [level for level, _ in sorted_items[:count]]
        return sorted(top_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels: dict[float, int] = {}
        
        # Current session levels - use top N instead of percentile
        for level in self._extract_top_levels(self._session_levels, self.top_levels_count):
            all_levels[level] = all_levels.get(level, 0) + 1
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            for level in self._extract_top_levels(session_profile, self.top_levels_count):
                all_levels[level] = all_levels.get(level, 0) + 1
        
        return sorted(list(all_levels.keys()))

    def _get_session_median_volume(self) -> float:
        """Get median volume of bars seen so far in this session."""
        if not self._session_volumes:
            return 0.0
        sorted_vols = sorted(self._session_volumes)
        return float(sorted_vols[len(sorted_vols) // 2])

    def _get_session_directional_bias(self) -> int:
        """Get net directional bias of session so far. >0 = upward, <0 = downward."""
        if not self._session_deltas:
            return 0
        return sum(self._session_deltas)

    def _has_momentum_confirmation(self, direction: str) -> bool:
        """Check if recent bars show momentum in the specified direction."""
        if len(self._recent_bars) < 2:
            return True  # Allow first bars
        
        # Check if at least one of the last 2 bars had the same directional delta
        for bar in self._recent_bars[:-1]:  # Exclude current bar
            if direction == "long" and bar.delta > 0:
                return True
            if direction == "short" and bar.delta < 0:
                return True
        
        return False

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with session bias filtering and momentum confirmation.
        
        Additional filters:
        1. Session directional bias: only take longs if session bias > 0,
           only take shorts if session bias < 0
        2. Momentum confirmation: at least one recent bar in same direction
        3. All previous filters from #81 (core proven filters)
        """
        if current.volume == 0:
            return None

        bar_range = current.high - current.low

        # PRE-FILTER 1: Bar range bounds
        if bar_range < self.min_bar_range or bar_range > self.max_bar_range:
            return None

        # PRE-FILTER 2: Tick count
        if current.tick_count < self.min_tick_count:
            return None

        # PRE-FILTER 3: Body ratio
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range if bar_range > 0 else 0
        if body_ratio < self.min_body_ratio:
            return None

        # PRE-FILTER 4: Volume context
        session_median = self._get_session_median_volume()
        if session_median > 0 and current.volume < session_median * self.volume_ratio_threshold:
            return None

        # Core delta_ratio check
        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check wick showing price rejection
        if delta > 0:  # Long: buying pressure, need lower wick
            lower_wick = current.close - current.low
            if lower_wick < self.min_wick_points:
                return None
        elif delta < 0:  # Short: selling pressure, need upper wick
            upper_wick = current.high - current.close
            if upper_wick < self.min_wick_points:
                return None
        else:
            return None

        # Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # NEW: Session directional bias filter
        session_bias = self._get_session_directional_bias()
        if delta > 0 and session_bias <= 0:  # Long but session biased down
            return None
        if delta < 0 and session_bias >= 0:  # Short but session biased up
            return None

        # NEW: Momentum confirmation
        direction_str = "long" if delta > 0 else "short"
        if not self._has_momentum_confirmation(direction_str):
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
