"""
Volume profile absorption strategy - investigating why #81 achieved -0.0096.

Campaign analysis reveals:
- #81: -0.0096 with 622 trades (BEST - 10x better than #76)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades

The previous trial produced byte-identical trades on synthetic data despite
implementing initiation confirmation logic. This reveals the core issue: the
initiation check (_check_initiation) was returning None because:

1. _absorption_candidates was being populated but never properly matched to
   current bars due to timestamp logic
2. The initiation check was too strict (requiring efficiency > avg * threshold)
3. The require_initiation flag was False by default, so the initiation path
   never executed

The real insight from #81's success: it likely uses a DIFFERENT approach to
level extraction and proximity that naturally filters for higher-quality setups.
Looking at the pattern:
- #81: 622 trades, -0.0096 score (best)
- #76: 1079 trades, -0.0893 score

#81 trades 43% less frequently but scores 10x better. This suggests it's
finding DIFFERENT, BETTER setups, not just filtering the same ones more strictly.

Key hypothesis: #81 probably:
1. Uses a tighter level proximity threshold (maybe 3-4 points instead of 6)
2. Extracts levels differently (maybe only carries the single POC, not top 6)
3. Has stricter absorption criteria that naturally filters for better quality
4. Uses a smaller target_multiple (maybe 1.5-2.0 instead of 2.5-4.0)

This trial implements a fundamentally different approach:
- Only carry POC (point of control) from each session, not top N levels
- Use much tighter proximity (3.0 points instead of 6.0)
- Stricter absorption_delta_ratio (0.45 instead of 0.35)
- Smaller target_multiple (1.5 instead of 2.5)
- Require higher wick points (0.75 instead of 0.50)
- Require minimum body points (0.75 instead of 0.50)
- Keep require_initiation=False (it wasn't working anyway)

The hypothesis: By being much more selective about WHICH setups to trade,
we naturally improve the quality of each trade, leading to better scores
even with fewer total trades.

Expected: Score better than #76's -0.0893, potentially approaching #81's -0.0096.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.45,
        level_proximity: float = 3.0,
        min_wick_points: float = 0.75,
        min_body_points: float = 0.75,
        min_tick_count: int = 10,
        stop_buffer: float = 0.25,
        target_multiple: float = 1.5,
        max_stop_distance: float = 3.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.min_body_points = min_body_points
        self.min_tick_count = min_tick_count
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_pocs: list[float] = []  # Keep POC from last 2 sessions
        
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
        """Extract POC from completed session and carry forward."""
        if self._session_levels:
            poc = self._extract_poc(self._session_levels)
            if poc is not None:
                self._prior_session_pocs.append(poc)
                # Keep only last 2 sessions' POCs
                if len(self._prior_session_pocs) > 2:
                    self._prior_session_pocs.pop(0)
        
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

    def _extract_poc(self, profile: dict[float, int]) -> Optional[float]:
        """Extract the point of control (highest volume level)."""
        if not profile:
            return None

        max_level = max(profile.items(), key=lambda x: x[1])
        return max_level[0]

    def _get_carried_levels(self) -> list[float]:
        """Get carried levels from current and prior sessions."""
        all_levels = []
        
        # Current session POC
        current_poc = self._extract_poc(self._session_levels)
        if current_poc is not None:
            all_levels.append(current_poc)
        
        # Prior session POCs
        all_levels.extend(self._prior_session_pocs)
        
        return all_levels

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect high-quality absorption at carried volume levels.
        
        Much stricter filters than previous attempts:
        - delta_ratio: 0.45 (very clean one-sided flow)
        - level_proximity: 3.0 points (tight proximity)
        - min_wick_points: 0.75 (clear price rejection)
        - min_body_points: 0.75 (meaningful bar body)
        - min_tick_count: 10 (active bar)
        
        This should naturally filter for the highest quality setups,
        similar to what #81 likely achieved.
        """
        if current.volume == 0:
            return None

        # Require minimum tick count
        if current.tick_count < self.min_tick_count:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Very strict one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Require meaningful bar body
        bar_body = abs(current.close - current.open)
        if bar_body < self.min_body_points:
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
