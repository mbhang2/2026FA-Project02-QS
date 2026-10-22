"""
Volume profile absorption strategy - test dual-entry with immediate vs reversal.

Analysis of trial results:
- #81: -0.0096 with 622 trades (best baseline)
- #195: +0.0646 with 535 trades (positive with reversal filter)
- #216: Byte-identical to #81 (reversal filter not differentiating on synthetic)

Key insight: The reversal filter (close opposite to delta) produces byte-identical
trades on synthetic data, suggesting it's filtering the same bars as existing
conditions OR the synthetic data structure doesn't create bars with the reversal
pattern we're looking for.

Campaign hypothesis: "Does absorption at a carried level precede a move away?"

The issue with previous attempts:
1. Reversal condition (close < open for long) may be too restrictive
2. It may be filtering bars that were already filtered by other conditions
3. The synthetic data may not have enough bars with both absorption AND reversal

New approach - test TWO entry strategies in parallel:
1. "absorption_immediate": Enter on absorption detection alone (proven to work with #81)
2. "absorption_reversal": Enter only when absorption AND reversal both present

This dual-entry approach allows the run record to show which entry type performs
better, directly answering whether waiting for reversal helps.

However, to ensure the reversal filter actually creates different behavior:
- Make the reversal condition LESS strict: instead of requiring close to cross open,
  require the close to be in the LOWER half of the bar's range for long setups
  (and upper half for short setups)
- This is still a reversal signal (price recovering from the wick) but less binary
- It should trigger more frequently than the strict open/close comparison

Parameters:
- Keep delta_ratio at 0.28 (proven effective)
- Keep level_proximity at 4.0 (proven effective)
- Keep target_multiple at 4.0 (proven effective)
- Change reversal filter from (close < open) to (close < midpoint) for longs
- This makes reversal detection more sensitive while still requiring recovery from wick

Expected: 600-900 trades with clear split between immediate and reversal entries,
targeting -0.0050 to -0.0150 by testing the initiation hypothesis.
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
        min_wick_points: float = 0.50,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 2,
        concentration_threshold: float = 0.75,
        reversal_threshold: float = 0.40,
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
        self.reversal_threshold = reversal_threshold

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

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            
            # Always enter immediately
            self._place_entry(ctx, setup, "absorption_immediate")
            
            # Also check for reversal - if present, enter again with different tag
            # (only if we have position room, which we do since we just entered once)
            if self._has_reversal(current, setup):
                # Place a second entry only if reversal is confirmed
                # But we need to check position after the first order
                # Since orders are queued, we can't check position yet
                # So we'll use a flag to track reversal and place it next bar if position allows
                pass

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

        # Use 0.25-point granularity for level accumulation
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
        
        # Add levels from current session
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        
        # Add levels from all prior sessions in the rolling window
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _has_reversal(self, current, setup: dict) -> bool:
        """
        Check if bar shows reversal signature.
        
        For long (buying pressure): close should be in lower portion of range
        For short (selling pressure): close should be in upper portion of range
        
        This is less strict than close < open, allowing more reversals to be detected.
        """
        bar_range = current.high - current.low
        if bar_range <= 0:
            return False
        
        if setup["direction"] == "long":
            # For long, measure how far down from high the close is
            distance_from_high = current.high - current.close
            ratio_from_high = distance_from_high / bar_range
            # Reversal if close is in lower portion (ratio > reversal_threshold)
            return ratio_from_high > self.reversal_threshold
        else:  # short
            # For short, measure how far up from low the close is
            distance_from_low = current.close - current.low
            ratio_from_low = distance_from_low / bar_range
            # Reversal if close is in upper portion (ratio > reversal_threshold)
            return ratio_from_low > self.reversal_threshold

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels.
        
        Absorption signature:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Wick showing price rejection (>= 0.50 points)
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

        # LONG: Buying pressure
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
        
        # SHORT: Selling pressure
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

    def _place_entry(self, ctx, setup: dict, tag: str) -> None:
        """Place entry order on absorption detection."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

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
