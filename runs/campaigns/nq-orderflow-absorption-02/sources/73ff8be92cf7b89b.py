"""
Volume profile absorption strategy - test with expanded level history and refined proximity.

Analysis of recent performance:
- #81: -0.0096 with 622 trades (BEST OVERALL)
- #163: -0.0746 with 876 trades
- #159: -0.0705 with 894 trades

Critical observation: The previous trial added min_level_sessions=2 filter to require
levels appear in 2+ sessions. However, on synthetic data it produced BYTE-IDENTICAL
trades (70 trades), meaning this filter never actually triggered - either:
1. The multi-session strength check is always passing (tolerance too loose), or
2. Absorption bars are never at multi-session levels in early sessions

The real issue: We're only keeping 2 prior sessions in history. On the very first
session(s), there ARE no prior sessions, so min_level_sessions=2 can never be met.
This means the filter only activates after session 3, making it ineffective as a
selectivity mechanism early in the run.

New hypothesis: Instead of requiring multi-session presence (which fails early),
we should EXPAND the history window to capture more sessions, then use a TIGHTER
proximity threshold as the real selectivity filter. More history + tighter proximity
should provide better level identification and fewer false entries.

This trial:
1. Expands prior session history from 2 to 5 sessions (more data for level strength)
2. Removes the min_level_sessions filter (it's ineffective due to initialization)
3. Tightens level_proximity from 2.0 to 1.5 (stricter proximity = fewer trades)
4. Increases absorption_delta_ratio from 0.32 to 0.35 (stricter delta filter)
5. Lowers concentration_threshold from 0.60 to 0.55 (more levels per session)
6. Keeps target_multiple=4.0 (proven parameter)

The expanded history window provides better level identification across more sessions,
and the tighter proximity (1.5 vs 2.0) becomes the real selectivity mechanism. This
should differentiate on both synthetic and real data by requiring price to be VERY
close to a carried level.

Expected: Score better than previous trials by combining better level identification
(5-session history) with stricter proximity filtering (1.5 points). Target: -0.0085
to -0.0110 with 450-600 trades.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        level_proximity: float = 1.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.55,
        max_prior_sessions: int = 5,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.concentration_threshold = concentration_threshold
        self.max_prior_sessions = max_prior_sessions

        # Level tracking with expanded multi-session history
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
            # Keep expanded history: last N sessions for better level identification
            if len(self._prior_session_levels) > self.max_prior_sessions:
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

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract levels where volume is above significance threshold."""
        if not profile:
            return []

        max_vol = max(profile.values()) if profile else 0
        if max_vol == 0:
            return []

        threshold_vol = max_vol * self.concentration_threshold
        selected_levels = [level for level, vol in profile.items() if vol >= threshold_vol]

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = set()
        
        # Current session levels
        all_levels.update(self._extract_significant_levels(self._session_levels))
        
        # Prior session levels (expanded history)
        for session_profile in self._prior_session_levels:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels with tight proximity filtering.
        
        Absorption signature requires:
        1. Very strong directional delta (delta_ratio >= 0.35)
        2. Price VERY near a carried level (within 1.5 points)
        
        The tighter proximity (1.5 vs 2.0) filters for higher-conviction setups
        where price is actually AT the level, not just nearby. Combined with
        expanded 5-session history, this provides better level identification
        and fewer false entries.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Very strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Check proximity to carried level - VERY TIGHT
        all_levels = self._get_carried_levels()
        if not all_levels:
            return None

        min_distance = min(abs(current.close - level) for level in all_levels)
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
