"""
Volume profile absorption strategy - fixing synthetic data identical trades issue.

The previous trial produced byte-identical trades on synthetic data because
require_initiation=False by default means it trades on absorption alone, which
is exactly what the synthetic data triggers.

The real issue: we need to understand WHY #81 achieved -0.0096 with 622 trades.
Looking at the campaign history:
- #81: -0.0096 with 622 trades (BEST)
- #86: -0.0600 with 673 trades  
- #76: -0.0893 with 1079 trades

#81 is 10x better than #76 despite similar trade count. This suggests the edge
comes from WHICH trades it takes, not how many. The parameters must be filtering
for higher quality setups.

Key observation: The previous trials all use similar absorption detection but
get different results. The difference must be in:
1. How levels are extracted/carried
2. How proximity is measured
3. Stop/target sizing

Looking at #81's likely approach:
- It probably uses a different level extraction method (maybe only the single
  strongest level per session, not top 8)
- It probably measures proximity differently (maybe as % of current price, not
  absolute points)
- It probably has tighter stop distances relative to the target

This trial implements a fundamentally different level selection:
- Only carry the SINGLE strongest level from each session (POC - point of control)
- Use percentage-based proximity instead of absolute points
- Tighter target_multiple relative to stop (2.0x instead of 4.0x)
- Slightly stricter delta_ratio (0.30 instead of 0.28)
- Add a volume-based filter: only trade at levels that had significant volume

Expected: Different trade distribution that should score better than #76's -0.0893.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        level_proximity_pct: float = 0.05,
        min_wick_points: float = -0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_level_volume: float = 0.15,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity_pct = level_proximity_pct
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.min_level_volume = min_level_volume

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[float] = []
        
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
            poc = self._extract_poc(self._session_levels)
            if poc is not None:
                self._prior_session_levels.append(poc)
                # Keep only last 2 sessions' POCs
                if len(self._prior_session_levels) > 2:
                    self._prior_session_levels.pop(0)
        
        # Reset for new session
        self._session_levels = {}
        self._entered_bar_timestamps = set()

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

    def _extract_poc(self, profile: dict[float, int]) -> Optional[float]:
        """Extract the point of control (single strongest level) from volume profile."""
        if not profile:
            return None

        # Find the level with the most volume
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
        all_levels.extend(self._prior_session_levels)
        
        return all_levels

    def _is_level_strong(self, level: float, profile: dict[float, int]) -> bool:
        """Check if a level has above-average volume."""
        if not profile:
            return False
        
        total_vol = sum(profile.values())
        if total_vol == 0:
            return False
        
        level_vol = profile.get(level, 0)
        avg_vol = total_vol / len(profile)
        
        return level_vol >= avg_vol * self.min_level_volume

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried volume levels.
        
        Key differences from previous attempts:
        - Uses percentage-based proximity instead of absolute points
        - Only considers strong volume levels
        - Slightly stricter delta_ratio (0.30)
        - Tighter target_multiple (2.0x)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Stricter threshold
        if delta_ratio < self.absorption_delta_ratio:
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

        # Check proximity to carried level using percentage distance
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        # Find closest level that is strong
        closest_level = None
        min_pct_distance = float('inf')
        
        for level in carried_levels:
            if not self._is_level_strong(level, self._session_levels):
                continue
            
            pct_distance = abs(current.close - level) / current.close
            if pct_distance < min_pct_distance:
                min_pct_distance = pct_distance
                closest_level = level

        if closest_level is None or min_pct_distance > self.level_proximity_pct:
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
