"""
Volume profile absorption strategy - refined to match #81's success pattern.

Campaign analysis reveals a clear pattern:
- #81: -0.0096 with 622 trades (BEST OVERALL)
- #146: -0.0611 with 907 trades
- #86: -0.0600 with 673 trades

All three top performers share these characteristics:
1. Moderate trade frequency (622-907 trades)
2. Strict absorption filtering (high delta_ratio)
3. Multi-session level carrying
4. Conservative stop placement

The key insight from #81's dominance: The combination of:
- absorption_delta_ratio = 0.28 (strict filtering for quality)
- level_proximity = 6.0 (reasonable proximity to levels)
- Simple, direct level extraction without complex thresholds
- Rolling 2-session window (not too many, not too few)

Previous trials with stricter proximity (4.0, 1.5) and higher concentration
thresholds reduced trades too much. #81's parameters hit the sweet spot.

This trial returns to #81's exact proven parameters with one refinement:
- Use a rolling window of 2 sessions (not 3) to stay closer to #81
- Keep absorption_delta_ratio at 0.28
- Keep level_proximity at 6.0
- Extract top 8 levels per session (most significant structure)
- Keep target_multiple at 4.0
- Ensure proper multi-session carrying

The hypothesis: #81 achieved -0.0096 with this exact configuration. By
implementing it precisely, we should achieve similar results (target: -0.0090
to -0.0100 range with 600-650 trades).

Key difference from recent trials: Simpler level extraction logic that
matches what actually worked, rather than adding complexity that hasn't
been validated.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 6.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 2,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.rolling_window_sessions = rolling_window_sessions

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        
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

        # Accumulate volume into current session profile
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
        """Handle session boundary - add completed session to rolling window."""
        if self._current_session_profile:
            self._session_profiles.append(dict(self._current_session_profile))
            # Keep only last N sessions in rolling window
            if len(self._session_profiles) > self.rolling_window_sessions:
                self._session_profiles.pop(0)
        
        # Reset for new session
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels for current session."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._current_session_profile[level_key] = (
                self._current_session_profile.get(level_key, 0) + int(vol_per_level)
            )
            level += 0.25

    def _extract_top_levels(self, profile: dict[float, int], top_n: int = 8) -> list[float]:
        """Extract the top N most significant levels from volume profile."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        top_levels = [level for level, _ in sorted_items[:top_n]]
        
        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """
        Get all carried levels from the rolling window of sessions.
        
        Combines levels from the current and prior sessions to identify
        multi-session price structure. Uses top-N extraction matching #81's
        approach.
        """
        all_levels = set()
        
        # Add levels from current session
        all_levels.update(self._extract_top_levels(self._current_session_profile, top_n=8))
        
        # Add levels from all prior sessions in the rolling window
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_top_levels(session_profile, top_n=8))
        
        return sorted(list(all_levels))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels using #81's proven approach.
        
        Absorption signature:
        - Strong directional delta (delta_ratio >= 0.28)
        - Price within 6.0 points of a carried level
        
        This is the exact filtering approach that achieved -0.0096 in #81.
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
