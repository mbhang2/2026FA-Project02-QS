"""
Volume profile absorption strategy - refined toward #81's -0.0096.

The synthetic test produced byte-identical trades (70), indicating the core logic
is sound. The issue: parameters are not selective enough to match #81's quality.

Analysis of top performers:
- #81: -0.0096 (622 trades) - BEST SCORE
- #86: -0.0600 (673 trades)
- #76: -0.0893 (1079 trades)

#81 achieves 12x better score than #76 with 43% fewer trades. This is not about
trading less—it's about trading ONLY the highest-conviction setups.

The byte-identical synthetic result means the baseline mechanism works. The real
data will show differences because the parameter tightening actually filters
differently on real price/volume patterns.

Key insight: The previous trial's byte-identical result suggests the tighter
parameters (delta_ratio 0.50, proximity 0.50) still produce the same 70 trades
on synthetic data. This means either:
1. The synthetic data doesn't trigger absorption patterns at all (likely)
2. The 70 trades are driven by a different code path

To move beyond byte-identical, we need to change the CORE DETECTION LOGIC,
not just parameters. The key difference between #81 and #76 is likely not just
stricter thresholds, but a different way of identifying what counts as absorption.

New hypothesis: #81 likely uses a multi-bar confirmation pattern, not just
single-bar absorption. A bar showing absorption followed by a bar that moves
efficiently away from the level is the real signal.

Changes for this trial:
1. Add lookback to detect multi-bar patterns (absorption + follow-through)
2. Require the bar AFTER absorption to show directional momentum
3. This fundamentally changes which bars trigger entries (not just stricter filtering)
4. Keep the strong level carrying logic from previous trial
5. Adjust parameters to be less extreme (0.40 delta_ratio, 1.0 proximity)
   to allow more setups, but filter them through the multi-bar pattern

Expected: Different trade count on real data (not byte-identical), with better
selectivity through pattern confirmation rather than just parameter tightening.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.40,
        level_proximity: float = 1.0,
        min_wick_points: float = 0.50,
        min_body_ratio: float = 0.20,
        stop_buffer: float = 0.20,
        target_multiple: float = 2.5,
        max_stop_distance: float = 1.5,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_tick_count: int = 8,
        min_accumulated_volume: int = 2000,
        require_follow_through: bool = True,
        follow_through_efficiency: float = 0.60,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.min_body_ratio = min_body_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_tick_count = min_tick_count
        self.min_accumulated_volume = min_accumulated_volume
        self.require_follow_through = require_follow_through
        self.follow_through_efficiency = follow_through_efficiency

        # Level tracking across sessions
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
        # Track absorption candidates waiting for follow-through
        self._absorption_candidates: dict = {}  # ts -> setup dict
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

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

        # Check if prior bar was absorption - if so, check for follow-through
        if self.require_follow_through and prior.close_ts in self._absorption_candidates:
            candidate_setup = self._absorption_candidates.pop(prior.close_ts)
            
            # Check if current bar shows follow-through
            if self._check_follow_through(prior, current, candidate_setup):
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, candidate_setup)
            return

        # Detect absorption on current bar
        setup = self._detect_absorption(current)
        if setup:
            if self.require_follow_through:
                # Store for next bar's follow-through check
                self._absorption_candidates[current.close_ts] = setup
            else:
                # No follow-through requirement, enter immediately
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
        self._absorption_candidates = {}

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

    def _get_strong_levels(self) -> list[float]:
        """
        Get levels with significant accumulated volume across all sessions.
        Only levels that have shown repeated importance are traded at.
        """
        all_level_volumes: dict[float, int] = {}
        
        # Current session
        for level, vol in self._session_levels.items():
            all_level_volumes[level] = all_level_volumes.get(level, 0) + vol
        
        # Prior sessions
        for session_profile in self._prior_session_levels:
            for level, vol in session_profile.items():
                all_level_volumes[level] = all_level_volumes.get(level, 0) + vol
        
        # Filter to only levels with accumulated volume
        strong_levels = [
            level for level, vol in all_level_volumes.items()
            if vol >= self.min_accumulated_volume
        ]
        
        return sorted(strong_levels)

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption bar at strong carried levels.
        
        This is the CANDIDATE bar - if follow_through is required,
        it will be confirmed by the next bar's behavior.
        """
        if current.volume == 0:
            return None

        # Require meaningful tick concentration
        if current.tick_count < self.min_tick_count:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # One-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # Require body significance
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range
        
        if body_ratio < self.min_body_ratio:
            return None

        # Require clear wick showing price rejection
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

        # Check proximity to strong carried levels
        strong_levels = self._get_strong_levels()
        if not strong_levels:
            return None

        min_distance = min(abs(current.close - level) for level in strong_levels)
        if min_distance > self.level_proximity:
            return None

        # Long setup
        if delta > 0:
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_bar": current,
            }

        # Short setup
        else:
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_bar": current,
            }

    def _check_follow_through(self, absorption_bar, current_bar, setup: dict) -> bool:
        """
        Check if the bar following absorption shows efficient follow-through.
        
        For a long setup: current bar should close higher and move efficiently
        (distance moved / flow behind it should be high).
        For a short setup: current bar should close lower with similar efficiency.
        """
        direction = setup["direction"]
        
        if direction == "long":
            # Follow-through: bar moves up efficiently with buying pressure
            if current_bar.close <= absorption_bar.close:
                return False
            
            # Check efficiency: distance moved per unit of volume
            move = current_bar.close - absorption_bar.close
            if current_bar.volume > 0:
                efficiency = move / current_bar.volume
                if efficiency < self.follow_through_efficiency / 1000:
                    return False
            
            # Prefer bars with buying pressure
            if current_bar.delta <= 0:
                return False
            
            return True
        
        else:  # Short
            # Follow-through: bar moves down efficiently with selling pressure
            if current_bar.close >= absorption_bar.close:
                return False
            
            # Check efficiency: distance moved per unit of volume
            move = absorption_bar.close - current_bar.close
            if current_bar.volume > 0:
                efficiency = move / current_bar.volume
                if efficiency < self.follow_through_efficiency / 1000:
                    return False
            
            # Prefer bars with selling pressure
            if current_bar.delta >= 0:
                return False
            
            return True

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
