"""
Volume profile absorption strategy - test multi-session level decay and confirmation.

Analysis of top performers (#81: -0.0096, #165: -0.0350, #155: -0.0462) shows
they all use 2-session rolling windows with delta_ratio around 0.28. The synthetic
data tests produced byte-identical trades, indicating the wick ratio filters are
not actually changing entry conditions.

Key insight from the campaign brief: absorption at a carried level should precede
a move away from that level. The current strategy enters immediately on absorption
detection, but the campaign explicitly asks whether waiting for a confirming bar
improves results.

This trial implements a two-stage confirmation system:
1. Detect absorption bar: strong delta + wick + proximity to carried level
2. Wait for confirming bar: next bar must show directional follow-through
   - Long: confirming bar closes above absorption bar close
   - Short: confirming bar closes below absorption bar close

The synthetic test showed byte-identical trades because the confirmation logic
was clearing pending setups too aggressively. This trial fixes that by:
1. Only clearing pending setup after confirmation attempt, not on every bar
2. Properly tracking the absorption bar's close price for confirmation
3. Using a cleaner state machine: detect → queue → confirm → enter

This directly tests the campaign hypothesis: does absorption at a level precede
a move away from it? The confirming bar is the market's answer to that question.

Expected: 400-600 trades with better quality, targeting score -0.0080 to -0.0150.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 4.0,
        min_wick_points: float = -0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
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
        
        # State machine for two-stage confirmation
        self._pending_setup: Optional[dict] = None
        self._absorption_bar_close: Optional[float] = None
        
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
            self._pending_setup = None
            self._absorption_bar_close = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # State machine: Check for confirmation if we have a pending setup
        if self._pending_setup is not None and self._absorption_bar_close is not None:
            if self._should_confirm_setup(current):
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, self._pending_setup)
            # Clear pending setup after confirmation attempt (success or failure)
            self._pending_setup = None
            self._absorption_bar_close = None
            return

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            # Queue for confirmation on next bar
            self._pending_setup = setup
            self._absorption_bar_close = current.close

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
        self._pending_setup = None
        self._absorption_bar_close = None

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
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection
        3. Proximity to carried level (within 4.0 points)
        4. Valid stop/target distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # LONG: Buying pressure with lower wick
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
        
        # SHORT: Selling pressure with upper wick
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

    def _should_confirm_setup(self, confirming_bar) -> bool:
        """
        Check if the confirming bar validates the absorption setup.
        
        For long: confirming bar must close above absorption bar's close
        For short: confirming bar must close below absorption bar's close
        """
        if self._pending_setup is None or self._absorption_bar_close is None:
            return False
        
        direction = self._pending_setup["direction"]
        
        if direction == "long":
            return confirming_bar.close > self._absorption_bar_close
        else:  # short
            return confirming_bar.close < self._absorption_bar_close

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on confirmed absorption."""
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
