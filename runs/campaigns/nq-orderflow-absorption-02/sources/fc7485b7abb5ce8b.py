"""
Volume profile absorption strategy - test stricter delta_ratio with tighter proximity.

Analysis of best performers:
- #81: -0.0096 with 622 trades (BEST OVERALL)
- #159: -0.0705 with 894 trades
- #155: -0.0462 with 859 trades

The synthetic test showed BYTE-IDENTICAL trades to the previous version,
confirming that the rolling window and directional filtering approach does not
differentiate on synthetic data. This suggests the synthetic data lacks sufficient
multi-session volume structure to test these refinements.

Key insight: #81's dominance at -0.0096 with only 622 trades suggests it has
the highest selectivity - it trades less but more profitably. The core parameters
that drive selectivity are:
1. absorption_delta_ratio: Higher threshold = fewer but higher-quality signals
2. level_proximity: Tighter = only trades near very strong levels
3. concentration_threshold: Stricter = only uses the most significant levels

Current parameters (delta_ratio=0.28, proximity=3.2, concentration=0.65) may still
be too permissive. #81 achieved -0.0096 with these exact parameters but on a
different implementation.

New hypothesis: The issue is not the parameters but the CONDITION for trading.
We should increase selectivity by:
1. Raising absorption_delta_ratio from 0.28 to 0.32 (stricter delta requirement)
2. Tightening level_proximity from 3.2 to 2.8 (only very close to levels)
3. Keeping concentration_threshold at 0.65 (already strict)
4. This creates a narrower, higher-quality signal set

The synthetic test shows 70 trades with byte-identical fills. To make a real change
that affects real data, we need to change the FILTERING CONDITIONS, not just the
level extraction. Stricter delta_ratio and proximity will naturally filter more
aggressively on real data where volume structure varies.

Expected: Score better than #81's -0.0096, with 450-550 trades on real data.
Stricter selectivity targets the highest-conviction setups.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.32,
        level_proximity: float = 2.8,
        min_wick_points: float = 0.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.65,
        rolling_window_sessions: int = 3,
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
        self.concentration_threshold = concentration_threshold
        self.rolling_window_sessions = rolling_window_sessions

        # Rolling window of session profiles (last N sessions)
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
        """
        Get all carried levels from the rolling window of prior sessions.
        
        Combines levels from the last N sessions to identify multi-session
        price structure without accumulating noise from very old sessions.
        """
        all_levels = set()
        
        # Add levels from all sessions in the rolling window
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _get_directionally_filtered_levels(
        self, current, all_levels: list[float]
    ) -> list[float]:
        """
        Filter carried levels based on directional bias.
        
        For long setups (delta > 0): prefer levels at or above current price
        For short setups (delta < 0): prefer levels at or below current price
        """
        if not all_levels:
            return all_levels
        
        delta = current.delta
        
        if delta > 0:
            # Long bias: prefer levels at or above current price
            biased = [level for level in all_levels if level >= current.close - 1.0]
            return biased if biased else all_levels
        elif delta < 0:
            # Short bias: prefer levels at or below current price
            biased = [level for level in all_levels if level <= current.close + 1.0]
            return biased if biased else all_levels
        
        return all_levels

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with stricter filtering for higher-conviction setups.
        
        Absorption signature:
        - Very strong directional delta (delta_ratio >= 0.32, stricter than before)
        - Price very near a directionally-filtered carried level (within 2.8 points, tighter)
        - This creates a narrower, higher-quality signal set
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Core filter: very strong directional delta (stricter threshold)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check proximity to directionally-filtered carried level (tighter)
        all_levels = self._get_carried_levels()
        if not all_levels:
            return None

        # Apply directional filter
        filtered_levels = self._get_directionally_filtered_levels(current, all_levels)
        if not filtered_levels:
            return None

        min_distance = min(abs(current.close - level) for level in filtered_levels)
        if min_distance > self.level_proximity:
            return None

        # Optional wick check if configured (default is 0.0, so this passes)
        if self.min_wick_points > 0.0:
            has_wick = False
            if delta > 0:  # Buying pressure, check for lower wick
                lower_wick = current.close - current.low
                if lower_wick >= self.min_wick_points:
                    has_wick = True
            elif delta < 0:  # Selling pressure, check for upper wick
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
