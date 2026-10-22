"""
Volume profile absorption strategy - test adaptive level decay and session weighting.

Campaign analysis of best performers:
- #81: -0.0096 with 622 trades (baseline)
- #165: -0.0350 with 658 trades
- #155: -0.0462 with 859 trades
- #202: -0.0641 with 985 trades
- #203: -0.0693 with 1053 trades

Recent attempts at level filtering (#204, #205) produced byte-identical trades,
indicating the changes don't actually filter different bars.

Key insight from byte-identical results: The issue is not the level extraction
logic itself, but rather that we're over-filtering. The best performers (#81-#203)
all use 2-session rolling windows with 0.75 concentration, but we keep getting
byte-identical results when we try to be MORE selective.

This suggests the problem is the opposite: we need to be LESS selective on which
levels matter, but MORE selective on the absorption signature itself.

New approach - focus on improving the absorption detection filters rather than
the level extraction:

1. Return to 2-session rolling window (proven in #81, #165, #155)
2. Return to 0.75 concentration threshold (proven baseline)
3. Tighten absorption delta_ratio from 0.28 to 0.32 (require stronger directional flow)
4. Increase level_proximity from 4.0 to 5.0 (allow wider net around levels)
5. Require MINIMUM wick size of 0.5 points (not -0.5, which is too loose)
6. Increase target_multiple from 4.0 to 4.5 (better reward/risk)

Hypothesis: The byte-identical results mean our level extraction is fine. The
problem is that we're detecting too much absorption. By:
- Requiring STRONGER delta (0.32 vs 0.28)
- Requiring MEANINGFUL wick (0.5 vs -0.5 = any wick)
- Better reward/risk (4.5 vs 4.0)

We filter for higher-quality absorption setups while keeping the proven level
methodology. This should produce DIFFERENT trades because the absorption filters
are actually tighter, even though the level extraction is unchanged.

The synthetic test SHOULD show different trades because:
- Higher delta_ratio threshold (0.32 vs 0.28)
- Minimum wick requirement (0.5 vs -0.5)
- This directly filters which absorption bars trigger

Expected: 500-650 trades with better quality, targeting score -0.0120 to -0.0200.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.32,
        level_proximity: float = 5.0,
        min_wick_points: float = 0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 2,
        concentration_threshold: float = 0.75,
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
        self.rolling_window_sessions = rolling_window_sessions
        self.concentration_threshold = concentration_threshold

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = -1
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

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels and enter immediately
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
            if len(self._session_profiles) > self.rolling_window_sessions:
                self._session_profiles.pop(0)
        
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
        """Extract levels representing the top concentration_threshold of volume."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        cumulative_vol = 0
        selected_levels = []
        
        for level, vol in sorted_items:
            selected_levels.append(level)
            cumulative_vol += vol
            if cumulative_vol >= total_vol * self.concentration_threshold:
                break

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from the rolling window of sessions."""
        all_levels = set()
        
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.32)
        2. Meaningful wick showing price rejection (>= 0.5 points)
        3. Proximity to carried level (within 5.0 points)
        4. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta (tightened to 0.32)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # LONG: Buying pressure with meaningful lower wick
        if delta > 0:
            lower_wick = current.close - current.low
            
            if lower_wick < self.min_wick_points:
                return None
            
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SHORT: Selling pressure with meaningful upper wick
        elif delta < 0:
            upper_wick = current.high - current.close
            
            if upper_wick < self.min_wick_points:
                return None
            
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

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
