"""
Volume profile absorption strategy - refined toward #81's -0.0096 performance.

Campaign analysis reveals a critical insight: #81 achieved -0.0096 with only 622
trades, while all subsequent attempts have failed to improve. The synthetic test
shows 70 identical trades, meaning parameter changes only manifest on real data.

Best performers:
- #81: -0.0096 with 622 trades (BEST - 9x better than #76)
- #86: -0.0600 with 673 trades
- #76: -0.0893 with 1079 trades

The current approach has been incrementally tightening parameters (delta_ratio
0.32→0.35, level_proximity 3.2→2.8) but producing identical synthetic trades.
This suggests we're at a local optimum where real-data structure is the limiting
factor, not parameter precision.

Key insight from campaign brief: absorption at a carried level precedes a move
away from it. The question is directional and structural. The brief also
emphasizes that rules must hold for moves BETWEEN levels, not trade the tape.

New approach - shift focus from delta_ratio strictness to level quality:

1. Reduce delta_ratio to 0.30
   - #81 likely used 0.28-0.32 range
   - Current 0.35 may be filtering out valid setups
   - 0.30 captures strong one-sided flow without over-filtering
   - Allows more setups to reach the level-proximity filter

2. Tighten level_proximity to 2.5
   - Price must be very close to actual carried level
   - This is the real discriminator - proximity matters more than delta
   - 2.5 ensures we're trading at structure, not near it

3. Reduce min_wick_points to 0.20
   - Some absorption shows minimal wick
   - Over-requiring wick evidence may eliminate valid setups
   - 0.20 allows more patterns while maintaining selectivity

4. Increase target_multiple to 3.0
   - #81 likely used 3.0-3.2 range for moves between levels
   - 2.8 may be too conservative for inter-level moves
   - 3.0 targets proper moves while staying achievable

5. Increase max_levels_per_session to 8
   - More levels captured = more setup opportunities
   - Keeps only highest volume levels (top 8)
   - Balances between specificity and opportunity

6. Increase max_stop_distance to 4.0
   - Allows stops that correspond to actual bar extremes
   - Wider stops are acceptable if they're structural
   - Avoids false positives from overly tight stops

The hypothesis: #81's success came from finding the right balance between
absorption selectivity (delta_ratio) and proximity to actual levels. By relaxing
delta_ratio slightly while tightening level_proximity, we allow more high-quality
setups to pass through to the level filter, which is the true discriminator.

Expected: Score at or near #81's -0.0096 with 650-750 trades.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.30,
        level_proximity: float = 2.5,
        min_wick_points: float = 0.20,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_levels_per_session: int = 8,
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
        """Extract the most significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)

        # Keep only top N most significant levels
        top_levels = [level for level, vol in sorted_items[:self.max_levels_per_session]]

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
        Detect absorption at carried volume levels.
        
        Balanced parameters targeting #81's performance:
        - delta_ratio: 0.30 (strong one-sided flow, not overly strict)
        - level_proximity: 2.5 (must be at actual carried level)
        - min_wick_points: 0.20 (evidence of rejection, not overly strict)
        - target_multiple: 3.0 (targets moves between levels)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Strong one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check proximity to carried level - this is the key discriminator
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Require some wick showing price rejection
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
