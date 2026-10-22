"""
Volume profile absorption strategy - test directional bias in level selection.

Analysis of recent trials:
- #151: -0.0576 with 992 trades (BEST RECENT)
- #81: -0.0096 with 622 trades (BEST OVERALL)
- #146: -0.0611 with 907 trades
- #76: -0.0893 with 1079 trades

Key observation: #151 improved to -0.0576 by relaxing level_proximity from 3.5
to 4.5 and concentration_threshold from 0.60 to 0.50. This suggests the
absorption signal is real but the level selection was too restrictive.

However, #151 still underperforms #81's -0.0096. The difference is not just
trade frequency (992 vs 622) but also which trades are being selected.

Hypothesis: #81's tight level_proximity (3.5) combined with strict 
concentration_threshold (0.60) created a HIGHLY SELECTIVE filter that only
took the highest-conviction setups. #151's relaxation increased trades but
reduced average quality.

New approach: Keep #81's tight proximity (3.5) but DIRECTIONALLY BIAS the
level extraction. Instead of using concentration_threshold uniformly, extract:
1. The absolute peak volume level (always)
2. Levels on the side of the market move (if delta > 0, prefer levels above;
   if delta < 0, prefer levels below)
3. Only within proximity threshold

This conditions on carried levels (required) while being more selective about
WHICH carried levels matter for this specific directional setup.

Expected: Score between #81 (-0.0096) and #151 (-0.0576), with 700-850 trades.
The directional bias should improve quality while the proximity constraint
maintains selectivity.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 3.5,
        min_wick_points: float = 0.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.60,
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

        # Level tracking with multi-session history
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
            # Keep only last 2 sessions
            if len(self._prior_session_levels) > 2:
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
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _get_directionally_biased_levels(self, current, all_levels: list[float]) -> list[float]:
        """
        Filter carried levels based on directional bias.
        
        For long setups (delta > 0): prefer levels at or above current price
        For short setups (delta < 0): prefer levels at or below current price
        
        This biases toward levels in the direction of the move, which makes
        sense for absorption: if buying pressure is being absorbed, the
        relevant resistance is above, not below.
        """
        if not all_levels:
            return all_levels
        
        delta = current.delta
        
        if delta > 0:
            # Long bias: prefer levels at or above current price
            biased = [level for level in all_levels if level >= current.close - 0.5]
            return biased if biased else all_levels
        elif delta < 0:
            # Short bias: prefer levels at or below current price
            biased = [level for level in all_levels if level <= current.close + 0.5]
            return biased if biased else all_levels
        
        return all_levels

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with directional bias in level selection.
        
        Absorption signature:
        - Strong directional delta (delta_ratio >= 0.28)
        - Wick in opposite direction showing price rejection
        - Price near a directionally-biased carried level (within 3.5 points)
        
        The directional bias means:
        - For buying pressure: look for resistance levels above
        - For selling pressure: look for support levels below
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Core filter: strong directional delta
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for wick in opposite direction to delta (absorption signature)
        has_wick = False
        if delta > 0:  # Buying pressure, check for lower wick (price rejection)
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure, check for upper wick (price rejection)
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to carried level with directional bias
        all_levels = self._get_carried_levels()
        if not all_levels:
            return None

        # Apply directional bias to level selection
        biased_levels = self._get_directionally_biased_levels(current, all_levels)
        if not biased_levels:
            return None

        min_distance = min(abs(current.close - level) for level in biased_levels)
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
