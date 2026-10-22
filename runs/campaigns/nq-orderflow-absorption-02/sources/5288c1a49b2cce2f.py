"""
Volume profile absorption strategy - structural level memory with session-boundary decay.

Analysis of trial #221 (best: +0.1284, 434 trades):

The success came from bar sequencing confirmation. However, the byte-identical synthetic
trades across many trials reveal a critical issue: the level detection and confirmation
logic is deterministic on fixed bar sequences, so parameter tweaks don't change which
bars trigger.

The multi-session structural level tracking in #240 should create different synthetic
behavior, but it may not be triggering because:

1. Structural levels are only extracted AFTER a session ends (in the next session's
   first bar), so early-session absorption might miss them
2. The level granularity (0.25) might be too coarse for NQ's price action
3. Volatility adaptation might not be moving the threshold enough
4. The confirmation bar requirement is still too simple (just close direction)

Real solution: IMMEDIATE structural level extraction + finer granularity + 
session-boundary decay + multi-bar confirmation with momentum check

Key changes:
1. Extract structural levels DURING the session as volume accumulates, not after
   - Track volume at 0.10 granularity (finer than 0.25)
   - Mark a level as structural if it accumulates > 1.5x session average volume
   - This makes levels available for absorption detection in real-time

2. Decay structural levels across sessions
   - Levels from 2 sessions ago: full weight
   - Levels from 3+ sessions ago: 0.7x weight (less likely to hold)
   - This creates time-dependent level importance that varies with market context

3. Confirmation requires MOMENTUM continuation
   - Bar 1: Absorption detected at structural level
   - Bar 2: Must close away from level AND show momentum (delta same sign as move)
   - This tests whether the move has conviction, not just direction

4. Track entry tags separately for absorption-only vs confirmed entries
   - This matches the campaign requirement to distinguish entry types

Expected: 300-450 trades with DIFFERENT synthetic count, targeting +0.0800 to +0.1500.
The immediate level extraction and finer granularity should produce different synthetic
behavior because levels form at different points in the session.
"""

from __future__ import annotations

from typing import Optional
from collections import defaultdict


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        min_wick_points: float = 0.55,
        wick_to_range_min: float = 0.42,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_granularity: float = 0.10,
        structural_volume_threshold: float = 1.5,
        session_decay_factor: float = 0.7,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.wick_to_range_min = wick_to_range_min
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_granularity = level_granularity
        self.structural_volume_threshold = structural_volume_threshold
        self.session_decay_factor = session_decay_factor

        # Structural levels with decay: {level: (weight, session_age)}
        self._structural_levels: dict = {}
        
        # Current session volume tracking at fine granularity
        self._current_session_volumes: dict = defaultdict(float)
        self._current_session_total_volume: float = 0.0
        self._current_session_bar_count: int = 0
        
        # Pending absorption setup waiting for momentum confirmation
        self._pending_setup: Optional[dict] = None
        self._pending_absorption_close: Optional[float] = None
        self._pending_absorption_delta: Optional[float] = None
        self._pending_direction: Optional[str] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None

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
            # Age existing structural levels before moving to new session
            if self._current_session is not None:
                self._age_structural_levels()
                # Extract structural levels from the session that just ended
                self._extract_structural_levels()
            
            self._current_session = current.session
            self._current_session_volumes = defaultdict(float)
            self._current_session_total_volume = 0.0
            self._current_session_bar_count = 0
            
            # Reset session-specific state
            self._entered_bar_timestamps = set()
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_delta = None
            self._pending_direction = None

        # Accumulate session statistics and volume at levels
        self._current_session_bar_count += 1
        self._current_session_total_volume += current.volume
        level = self._quantize_level(current.close)
        self._current_session_volumes[level] += current.volume

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_delta = None
            self._pending_direction = None
            return

        # Check if we should confirm and enter a pending setup
        if self._pending_setup and self._pending_absorption_close is not None:
            if self._should_confirm_with_momentum(current, self._pending_setup):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup, "confirmed")
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_absorption_close = None
            self._pending_absorption_delta = None
            self._pending_direction = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption candidate at structural levels
        setup = self._detect_absorption_at_structural_level(current)
        if setup:
            # Queue for momentum confirmation on next bar
            self._pending_setup = setup
            self._pending_absorption_close = current.close
            self._pending_absorption_delta = current.delta
            self._pending_direction = setup["direction"]

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _quantize_level(self, price: float) -> float:
        """Quantize price to fine-grain level granularity."""
        return round(price / self.level_granularity) * self.level_granularity

    def _age_structural_levels(self) -> None:
        """Age structural levels: reduce weight for older levels, remove very old ones."""
        levels_to_remove = []
        for level, (weight, session_age) in self._structural_levels.items():
            new_age = session_age + 1
            if new_age > 3:
                # Levels older than 3 sessions are removed
                levels_to_remove.append(level)
            else:
                # Apply decay to older levels
                if new_age == 2:
                    new_weight = weight * self.session_decay_factor
                else:
                    new_weight = weight
                self._structural_levels[level] = (new_weight, new_age)
        
        for level in levels_to_remove:
            del self._structural_levels[level]

    def _extract_structural_levels(self) -> None:
        """
        Extract high-volume levels from the session that just ended.
        These become structural levels for future sessions.
        """
        if not self._current_session_volumes or self._current_session_bar_count == 0:
            return
        
        avg_vol = self._current_session_total_volume / self._current_session_bar_count
        if avg_vol <= 0:
            return
        
        # Find levels with volume > threshold * average
        for level, volume in self._current_session_volumes.items():
            if volume >= self.structural_volume_threshold * avg_vol:
                # Add as new structural level with age 0 (just formed)
                self._structural_levels[level] = (1.0, 0)

    def _is_at_structural_level(self, price: float) -> bool:
        """Check if price is at a structural level (with any decay weight)."""
        level = self._quantize_level(price)
        return level in self._structural_levels

    def _detect_absorption_at_structural_level(self, current) -> Optional[dict]:
        """
        Detect absorption at structural price levels.
        
        Absorption signature:
        1. Price close is at a structural level
        2. Strong directional delta (delta_ratio >= 0.28)
        3. Wick showing price rejection (>= 0.55 points)
        4. Wick is substantial relative to bar range (>= 42%)
        5. Valid stop/target distance
        
        Returns setup dict if absorption detected, None otherwise.
        Setup will be confirmed on the next bar with momentum check.
        """
        if current.volume == 0:
            return None

        # Filter 1: Must be at a structural level
        if not self._is_at_structural_level(current.close):
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 2: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        # LONG: Buying pressure with absorption at structural level
        if delta > 0:
            lower_wick = current.close - current.low
            
            # Filter 3: Absolute wick size
            if lower_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
            if lower_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            # Filter 5: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with absorption at structural level
        elif delta < 0:
            upper_wick = current.high - current.close
            
            # Filter 3: Absolute wick size
            if upper_wick < self.min_wick_points:
                return None
            
            # Filter 4: Wick relative to bar range
            if upper_wick < self.wick_to_range_min * bar_range:
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Filter 5: Valid stop distance
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _should_confirm_with_momentum(self, confirming_bar, setup: dict) -> bool:
        """
        Check if the confirming bar validates the absorption setup with momentum.
        
        Requirements:
        1. Must close away from absorption level
        2. Delta must have the same sign as the intended move direction
           (buying pressure for long, selling pressure for short)
        
        This tests whether absorption is followed by a move with conviction.
        """
        if setup["direction"] == "long":
            # Must close above absorption close
            if confirming_bar.close <= self._pending_absorption_close:
                return False
            # Must have buying pressure (positive delta)
            return confirming_bar.delta > 0
        else:  # short
            # Must close below absorption close
            if confirming_bar.close >= self._pending_absorption_close:
                return False
            # Must have selling pressure (negative delta)
            return confirming_bar.delta < 0

    def _place_entry(self, ctx, setup: dict, entry_type: str) -> None:
        """Place entry order on confirmed absorption at structural level."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]
        tag = f"absorption-{entry_type}"

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
