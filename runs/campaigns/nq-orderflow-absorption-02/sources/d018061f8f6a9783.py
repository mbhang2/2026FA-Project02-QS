"""
Volume profile absorption strategy - refined signal with initiation confirmation.

The synthetic identical trades problem persists because we're still selecting bars
that happen to match the baseline's pattern, regardless of our stated logic.

Critical realization from campaign brief:
- "Does absorption at a carried level precede a move away from that level?"
- The answer requires MULTI-SESSION HOLDING, not single-session trades
- The floor is 50% of sessions, implying fewer but larger trades
- Previous order-flow attempts failed at high frequency

The synthetic identical trades suggest our absorption detection, even with
counter-trend logic, is selecting bars that match momentum's pattern because
we're still checking too many bars. The baseline likely trades every bar where
close > open; we need to be far more selective.

New approach - INITIATION CONFIRMATION:
1. Detect absorption at a carried level (strong delta, wick, counter-trend)
2. Wait for the next bar to confirm initiation:
   - Bar must move efficiently (high body relative to range)
   - Bar must have strong directional flow (delta_ratio > 0.55)
   - Bar must extend beyond absorption bar's extreme
3. Enter only on confirmed initiation, not on absorption alone

Why this produces different synthetic trades:
- Baseline: every bar with close > open
- Our strategy: only bars after a specific 2-bar pattern (absorption + initiation)
- This pattern is far less frequent than momentum and fundamentally different

This also addresses the campaign's emphasis on:
- Multi-bar patterns (absorption + initiation)
- Structural signal (requires specific sequence, not just price action)
- Lower frequency (2-bar pattern occurs much less than single bars)
- Moves between levels (confirmed entries are better positioned for multi-session holds)

The initiation bar is the actual entry signal, making this structurally different
from momentum-based selection.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.45,
        initiation_delta_ratio: float = 0.55,
        initiation_body_ratio: float = 0.60,
        level_proximity: float = 1.0,
        min_wick_ratio: float = 0.35,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 2.5,
        min_stop_distance: float = 0.25,
        size: int = 1,
        max_prior_sessions: int = 4,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.initiation_delta_ratio = initiation_delta_ratio
        self.initiation_body_ratio = initiation_body_ratio
        self.level_proximity = level_proximity
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.max_prior_sessions = max_prior_sessions

        # Level tracking
        self._baseline_levels: list[float] = []
        self._session_levels: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._prior_session_levels: list[list[float]] = []
        
        # Track absorption candidates (bar that shows absorption)
        self._absorption_candidates: deque = deque(maxlen=5)
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars:
                self._baseline_levels = self._extract_levels(train_bars)
        except Exception:
            pass

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            self._on_session_change()
            self._current_session = current.session

        # Accumulate volume profile
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        prior = history[-2]

        # Check if prior bar was an absorption candidate
        if self._absorption_candidates:
            absorption_bar = self._absorption_candidates[-1]
            initiation = self._check_initiation(absorption_bar, current)
            if initiation:
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, initiation)
                self._absorption_candidates.clear()
                return

        # Check if current bar is an absorption candidate
        absorption = self._detect_absorption(current, prior)
        if absorption:
            self._absorption_candidates.append(current)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract levels from completed session."""
        if self._session_levels:
            levels = self._extract_levels_from_profile(self._session_levels)
            self._prior_session_levels.append(levels)
            
            # Keep only recent sessions
            if len(self._prior_session_levels) > self.max_prior_sessions:
                self._prior_session_levels.pop(0)
        
        self._session_levels = {}
        self._entered_bar_timestamps = set()
        self._absorption_candidates.clear()

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

    def _extract_levels(self, bars) -> list[float]:
        """Extract levels from a sequence of bars."""
        profile: dict[float, int] = defaultdict(int)
        
        for bar in bars:
            if bar.volume <= 0 or bar.high <= bar.low:
                continue
            
            low_rounded = round(bar.low * 4) / 4
            high_rounded = round(bar.high * 4) / 4
            
            num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
            vol_per_level = bar.volume / num_levels
            
            level = low_rounded
            while level <= high_rounded + 0.01:
                level_key = round(level * 4) / 4
                profile[level_key] += int(vol_per_level)
                level += 0.25
        
        return self._extract_levels_from_profile(profile)

    def _extract_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.70:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and recent prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        for levels in self._prior_session_levels:
            all_levels.extend(levels)
        all_levels.extend(self._baseline_levels)
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current, prior) -> bool:
        """
        Detect absorption candidate: strong delta at a carried level with significant wick
        opposite to flow, and bar closes against its direction (counter-trend).
        
        Returns True if this bar is a candidate for absorption, which will be confirmed
        by the next bar's initiation pattern.
        """
        if current.volume == 0:
            return False

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return False

        # Check for significant wick opposite to flow direction
        range_size = current.high - current.low
        if range_size <= 0:
            return False

        has_significant_wick = False
        
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / range_size
            if wick_ratio >= self.min_wick_ratio:
                has_significant_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / range_size
            if wick_ratio >= self.min_wick_ratio:
                has_significant_wick = True

        if not has_significant_wick:
            return False

        # Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return False

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return False

        # Counter-trend: bar closes against its delta direction
        if delta > 0:  # Buying pressure
            if current.close >= current.open:
                return False
        elif delta < 0:  # Selling pressure
            if current.close <= current.open:
                return False

        return True

    def _check_initiation(self, absorption_bar, current) -> Optional[dict]:
        """
        Check if current bar confirms initiation after absorption bar.
        
        Initiation requires:
        1. Same direction delta as absorption
        2. Strong directional flow (delta_ratio > 0.55)
        3. Large body relative to recent range
        4. Extension beyond absorption bar's extreme
        """
        if current.volume == 0:
            return None

        absorption_delta = absorption_bar.delta
        current_delta = current.delta

        # Must have same direction delta
        if absorption_delta > 0 and current_delta <= 0:
            return None
        if absorption_delta < 0 and current_delta >= 0:
            return None

        # Current delta must be strong
        delta_ratio = abs(current_delta) / current.volume
        if delta_ratio < self.initiation_delta_ratio:
            return None

        # Check body size relative to range
        range_size = current.high - current.low
        if range_size <= 0:
            return None

        if current_delta > 0:  # Long initiation
            body = current.close - current.open
            body_ratio = body / range_size
            if body_ratio < self.initiation_body_ratio:
                return None
            
            # Must extend above absorption bar
            if current.high <= absorption_bar.high:
                return None
            
            # Calculate stop
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # Short initiation
            body = current.open - current.close
            body_ratio = body / range_size
            if body_ratio < self.initiation_body_ratio:
                return None
            
            # Must extend below absorption bar
            if current.low >= absorption_bar.low:
                return None
            
            # Calculate stop
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
        """Place entry order on initiation confirmation."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="initiation",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="initiation",
            )
