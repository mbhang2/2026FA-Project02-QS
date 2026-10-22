"""
Volume profile absorption strategy - refinement targeting score improvement.

Campaign analysis of proven approaches:
- #81 scored -0.0096 with 622 trades (BEST RECENT)
- #76 scored -0.0893 with 1079 trades (BEST OVERALL)
- #74 scored -0.0980 with 2763 trades

The current strategy produces identical synthetic trades, indicating the core
logic is sound. The challenge is that parameter tuning on synthetic data cannot
reveal what works on real data with volume structure.

Key insight from the campaign brief: "absorption at a carried level precedes a
move away from it" - the question is directional and structural, not just about
flow ratios. The brief also emphasizes the cost problem: rules must hold for
moves between levels, not trade the tape itself.

Critical observations from prior trials:
1. #81 at 622 trades with -0.0096 is the best recent score
2. #76 at 1079 trades with -0.0893 is the best overall - higher trade count
3. The difference suggests there's a sweet spot around 800-1200 trades
4. Synthetic data shows 70 trades consistently - this is the noise floor

The current parameters (delta_ratio=0.25, level_proximity=4.0, target_multiple=3.5)
are calibrated toward #81's sparse approach. But #76's better overall score with
more trades suggests we should shift slightly toward capturing more absorption
while maintaining quality.

This trial makes targeted adjustments:

1. delta_ratio: 0.22 (slightly relaxed from 0.25)
   - Captures more absorption patterns without being loose
   - Balances between #81's strictness (0.28) and #74's looseness (0.20)

2. level_proximity: 3.5 (tighter from 4.0)
   - Price must be closer to actual carried level
   - Reduces false positives from near-level absorption

3. target_multiple: 4.0 (increased from 3.5)
   - #76 used 4.0 and achieved better overall score
   - Targets moves between levels rather than small profits

4. max_stop_distance: 3.5 (tightened from 4.0)
   - Avoid wide stops that don't survive commission
   - Keeps risk-reward ratio favorable

5. Carry 2 prior sessions (unchanged)
   - Maintains structural memory of levels

6. Extract 8 levels per session (unchanged)
   - Focused set of significant levels

The hypothesis: #76's success came from slightly looser delta_ratio (0.28 vs 0.25
was too strict) combined with tighter level proximity and higher target multiple.
By moving toward #76's parameters while keeping the core logic from #81, we should
capture more valid setups while maintaining quality.

Expected outcome: Score between #81 and #76, with 800-1100 trades.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.22,
        level_proximity: float = 3.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 3.5,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Level tracking with session history
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []  # Keep last 2 sessions
        
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
            # Keep last 2 sessions
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

        # Keep top 8 most significant levels
        top_levels = [level for level, vol in sorted_items[:8]]

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        
        # Current session levels
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        
        # Prior session levels (2 sessions back)
        for session_levels in self._prior_session_levels:
            all_levels.extend(session_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption based on strong one-sided delta at carried levels.
        
        Key parameters calibrated toward #76's success pattern:
        - delta_ratio: 0.22 (slightly relaxed for more capture)
        - level_proximity: 3.5 (tighter to actual levels)
        - target_multiple: 4.0 (targets moves between levels)
        - max_stop_distance: 3.5 (tighter to avoid wide stops)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Delta-based selectivity
        if delta_ratio < self.absorption_delta_ratio:
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
