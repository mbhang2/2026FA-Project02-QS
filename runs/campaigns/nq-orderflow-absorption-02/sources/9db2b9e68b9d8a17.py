"""
Volume profile absorption strategy - test directional bias with adaptive selectivity.

Analysis of top performers:
- #81: -0.0096 with 622 trades (BEST OVERALL)
- #159: -0.0705 with 894 trades
- #155: -0.0462 with 859 trades

The synthetic test shows BYTE-IDENTICAL trades, confirming that parameter changes
in delta_ratio and level_proximity do not affect synthetic data generation.

Critical observation: #81 achieved the best score with only 622 trades across 384
sessions. The current strategy also produces identical synthetic trades, meaning
the extraction and clustering methods are not the real differentiator.

Key insight from campaign history: Directional bias in level selection (#155, #158)
improved trade quality. The best performers combined:
1. Strict delta_ratio filtering (0.28-0.32)
2. Tight level proximity (2.5-3.0 points)
3. Directional filtering of carried levels
4. Adaptive stop sizing based on bar structure

New hypothesis: Instead of pushing delta_ratio higher (0.32) and proximity tighter
(2.5), which may over-filter on real data, combine:
1. Keep delta_ratio at 0.28 (proven by #81)
2. Keep level_proximity at 3.0 (proven by #81/#159)
3. Add directional bias to filter levels (worked in #155/#158)
4. Implement adaptive stop sizing: use the bar's actual wick as the stop distance
   instead of a fixed buffer, which should better capture market structure

The adaptive stop approach:
- For long: stop = close - (close - low) * 1.2 (slightly beyond the bar's low)
- For short: stop = close + (high - close) * 1.2 (slightly beyond the bar's high)

This makes the stop distance data-driven rather than fixed, potentially improving
win rate by respecting actual price rejection patterns.

Expected: Score near #81's -0.0096 with 600-750 trades by combining #81's core
parameters with directional bias and adaptive stop sizing.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 3.0,
        min_wick_points: float = 0.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        top_levels_per_session: int = 10,
        cluster_distance: float = 0.5,
        adaptive_stop_multiplier: float = 1.2,
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
        self.top_levels_per_session = top_levels_per_session
        self.cluster_distance = cluster_distance
        self.adaptive_stop_multiplier = adaptive_stop_multiplier

        # Level tracking with multi-session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
        # Track bars we've entered on to avoid re-entry
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

        # Accumulate volume profile for current session
        self._update_session_profile(current)

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
            # Keep only last 2 sessions
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
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
        """Extract top N levels by volume."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        top_levels = [level for level, _ in sorted_items[:count]]
        
        return sorted(top_levels)

    def _cluster_levels(self, levels: list[float]) -> list[float]:
        """Cluster nearby levels to reduce redundancy."""
        if not levels:
            return []
        
        sorted_levels = sorted(levels)
        clusters = []
        current_cluster = [sorted_levels[0]]
        
        for level in sorted_levels[1:]:
            if level - current_cluster[-1] <= self.cluster_distance:
                current_cluster.append(level)
            else:
                # Finalize current cluster with average
                cluster_center = sum(current_cluster) / len(current_cluster)
                clusters.append(cluster_center)
                current_cluster = [level]
        
        # Finalize last cluster
        if current_cluster:
            cluster_center = sum(current_cluster) / len(current_cluster)
            clusters.append(cluster_center)
        
        return clusters

    def _get_directionally_filtered_levels(self, current, all_levels: list[float]) -> list[float]:
        """
        Filter carried levels based on directional bias.
        
        For long setups (delta > 0): prefer levels at or above current price
        For short setups (delta < 0): prefer levels at or below current price
        """
        if not all_levels:
            return all_levels
        
        delta = current.delta
        
        if delta > 0:
            # Long bias: prefer levels at or above current price
            biased = [level for level in all_levels if level >= current.close - 1.0]
            return biased if biased else all_levels
        elif delta < 0:
            # Short bias: prefer levels at or below current price
            biased = [level for level in all_levels if level <= current.close + 1.0]
            return biased if biased else all_levels
        
        return all_levels

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        
        # Current session levels
        current_top = self._extract_top_levels(self._session_levels, self.top_levels_per_session)
        all_levels.extend(current_top)
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            prior_top = self._extract_top_levels(session_profile, self.top_levels_per_session)
            all_levels.extend(prior_top)
        
        # Cluster the combined levels to reduce noise
        clustered = self._cluster_levels(all_levels)
        return clustered

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with directional bias and adaptive stop sizing.
        
        Absorption signature:
        - Strong directional delta (delta_ratio >= 0.28)
        - Price near a directionally-filtered carried level (within 3.0 points)
        - Adaptive stop based on bar's wick structure
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Core filter: strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check proximity to carried level
        all_levels = self._get_carried_levels()
        if not all_levels:
            return None

        # Apply directional filter to reduce false signals
        filtered_levels = self._get_directionally_filtered_levels(current, all_levels)
        if not filtered_levels:
            return None

        min_distance = min(abs(current.close - level) for level in filtered_levels)
        if min_distance > self.level_proximity:
            return None

        # Optional wick check if configured
        if self.min_wick_points > 0.0:
            has_wick = False
            if delta > 0:  # Buying pressure, check for lower wick
                lower_wick = current.close - current.low
                if lower_wick >= self.min_wick_points:
                    has_wick = True
            elif delta < 0:  # Selling pressure, check for upper wick
                upper_wick = current.high - current.close
                if upper_wick >= self.min_wick_points:
                    has_wick = True
            
            if not has_wick:
                return None

        # Calculate adaptive stop and target
        if delta > 0:  # Long
            # Adaptive stop: use bar's wick with multiplier
            bar_wick = current.close - current.low
            adaptive_stop_distance = bar_wick * self.adaptive_stop_multiplier
            stop_distance = max(adaptive_stop_distance, self.min_stop_distance)
            stop_distance = min(stop_distance, self.max_stop_distance)
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # Short
            # Adaptive stop: use bar's wick with multiplier
            bar_wick = current.high - current.close
            adaptive_stop_distance = bar_wick * self.adaptive_stop_multiplier
            stop_distance = max(adaptive_stop_distance, self.min_stop_distance)
            stop_distance = min(stop_distance, self.max_stop_distance)
            
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
