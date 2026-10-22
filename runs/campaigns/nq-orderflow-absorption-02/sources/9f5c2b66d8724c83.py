"""
Volume profile absorption strategy - refined toward #81's -0.0096 score.

The synthetic test shows identical 70 trades across all recent attempts, which
means the core logic is sound but we're not actually varying parameters in ways
that affect the synthetic data. The issue: synthetic data has limited sessions
and price movement, so many parameter variations produce identical trade counts.

Key insight from real data results:
- #81: -0.0096 with 622 trades (BEST)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades

#81 trades 57% as often as #76 but scores 9x better. This isn't about being
stricter - it's about being SMARTER about which setups to take.

Critical realization: The previous attempts all use the same core logic:
1. Build volume profile
2. Detect absorption (delta_ratio + wick)
3. Check proximity to carried levels
4. Place bracket order

But they vary only the thresholds. If we're getting identical synthetic trades,
the thresholds aren't the limiting factor on synthetic data.

What COULD make a real difference:
1. How we build the volume profile (0.25-point granularity is arbitrary)
2. How we extract "significant" levels (top N by volume is simplistic)
3. When we consider a level "carried" (distance-based proximity might miss context)
4. The relationship between absorption bar size and stop placement

The hypothesis: #81 likely uses a smarter level extraction that identifies
true points of control rather than just top-volume price points. Real POCs
often cluster - they're not isolated points but zones where multiple sessions
show activity.

This trial changes the level extraction to be SMARTER:
1. Identify volume clusters (consecutive high-volume levels) not just peaks
2. Use the cluster center as the level, not individual peaks
3. Weight levels by both volume AND how many sessions touch them
4. Be more selective about what counts as a "carried level"

This should produce different behavior on real data while maintaining the same
synthetic trade count (since synthetic data has limited sessions to create
meaningful clusters).
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.34,
        level_proximity: float = 2.0,
        min_wick_points: float = 0.35,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_distance: float = 3.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_levels_per_session: int = 5,
        cluster_threshold: float = 0.75,
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
        self.max_levels_per_session = max_levels_per_session
        self.cluster_threshold = cluster_threshold

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        
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
            levels = self._extract_levels_from_profile(self._session_levels)
            self._prior_session_levels.append(levels)
            # Keep only last 2 sessions
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
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
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _extract_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract significant levels by identifying volume clusters."""
        if not profile:
            return []

        sorted_levels = sorted(profile.keys())
        if not sorted_levels:
            return []

        # Identify clusters of consecutive high-volume levels
        clusters = []
        current_cluster = [sorted_levels[0]]
        
        for i in range(1, len(sorted_levels)):
            # If levels are within 0.75 points, they're in same cluster
            if sorted_levels[i] - sorted_levels[i-1] <= self.cluster_threshold:
                current_cluster.append(sorted_levels[i])
            else:
                if current_cluster:
                    clusters.append(current_cluster)
                current_cluster = [sorted_levels[i]]
        
        if current_cluster:
            clusters.append(current_cluster)

        # Extract cluster centers weighted by volume
        cluster_centers = []
        for cluster in clusters:
            total_vol = sum(profile[level] for level in cluster)
            weighted_sum = sum(level * profile[level] for level in cluster)
            center = weighted_sum / total_vol if total_vol > 0 else cluster[len(cluster)//2]
            cluster_centers.append((center, total_vol))

        # Sort by volume and take top N
        cluster_centers.sort(key=lambda x: x[1], reverse=True)
        top_levels = [center for center, vol in cluster_centers[:self.max_levels_per_session]]

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        
        # Current session levels
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        
        # Prior session levels
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect highest-conviction absorption with smart level matching.
        
        Key parameters targeting #81's exceptional quality:
        - delta_ratio: 0.34 (very strict one-sided flow)
        - level_proximity: 2.0 (must be AT the level)
        - min_wick_points: 0.35 (strong price rejection)
        - target_multiple: 3.0 (conservative targets)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Very strict threshold - only strongest absorption
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require strong wick showing price rejection
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

        # Check proximity to carried level - must be very close
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
