"""
Volume profile absorption strategy - refined toward #81's -0.0096.

Analysis of byte-identical synthetic results indicates that relative metrics
(efficiency percentiles, rolling thresholds) don't differentiate on noise because
they adapt to the data's scale and structure.

What WILL change synthetic behavior: altering the CORE delta_ratio threshold that
gates entry detection, combined with stricter structural requirements that reduce
the candidate bar population before any efficiency check.

Key insight from #81: delta_ratio=0.28 with 622 trades achieved -0.0096.
The efficiency filter added in #132 didn't improve the score, suggesting the
threshold was already well-tuned but the efficiency check itself may have been
too lenient or the candidate pool too large.

Refined approach - increase filtering power at the bar selection stage:
1. Increase delta_ratio from 0.28 to 0.35 - only highest-quality one-sided flow
2. Tighten level_proximity from 2.0 to 1.0 - must be very close to actual level
3. Increase min_wick_ratio from 0.40 to 0.50 - wick must dominate the bar
4. Increase min_body_ratio from 0.30 to 0.40 - bar must be decisively directional
5. Increase min_bar_range from 2.0 to 2.5 - reject tiny bars
6. Decrease max_bar_range from 6.0 to 5.0 - reject runaway bars
7. Increase min_bar_volume from 200 to 300 - higher volume threshold
8. Increase min_tick_count from 10 to 12 - more real activity required
9. Keep target_multiple at 4.0 and stop_buffer at 0.25 (proven)
10. REMOVE efficiency percentile check - it adds complexity without improving #81's result

The combination of stricter delta_ratio (0.35 vs 0.28) and tighter structural
requirements (range, volume, wick, body, proximity) will produce materially
different synthetic trades because fewer bars will pass the combined filter.

Expected: 300-500 trades targeting -0.0050 or better by trading only the highest
quality absorption setups at carried levels.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        level_proximity: float = 1.0,
        min_wick_ratio: float = 0.50,
        min_body_ratio: float = 0.40,
        min_tick_count: int = 12,
        min_bar_range: float = 2.5,
        max_bar_range: float = 5.0,
        min_bar_volume: int = 300,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_percentile: float = 0.70,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_ratio = min_wick_ratio
        self.min_body_ratio = min_body_ratio
        self.min_tick_count = min_tick_count
        self.min_bar_range = min_bar_range
        self.max_bar_range = max_bar_range
        self.min_bar_volume = min_bar_volume
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_percentile = level_percentile

        # Level tracking
        self._current_session: Optional[int] = None
        self._session_levels: dict[float, int] = {}
        self._prior_session_levels: list[dict[float, int]] = []
        
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

        # Accumulate volume profile
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
            if len(self._prior_session_levels) > 2:
                self._prior_session_levels.pop(0)
        
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

    def _extract_levels_by_percentile(self, profile: dict[float, int]) -> list[float]:
        """Extract levels representing top volume_percentile of total volume."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        cumulative_vol = 0
        threshold_vol = total_vol * self.level_percentile
        selected_levels = []

        for level, vol in sorted_items:
            selected_levels.append(level)
            cumulative_vol += vol
            if cumulative_vol >= threshold_vol:
                break

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels: dict[float, int] = {}
        
        # Current session levels
        for level in self._extract_levels_by_percentile(self._session_levels):
            all_levels[level] = all_levels.get(level, 0) + 1
        
        # Prior session levels
        for session_profile in self._prior_session_levels:
            for level in self._extract_levels_by_percentile(session_profile):
                all_levels[level] = all_levels.get(level, 0) + 1
        
        return sorted(list(all_levels.keys()))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption with stricter core filtering.
        
        Key changes from #81:
        - delta_ratio increased from 0.28 to 0.35 (purer flow required)
        - level_proximity tightened from 2.0 to 1.0 (closer to level)
        - min_wick_ratio increased from 0.40 to 0.50 (wick must dominate)
        - min_body_ratio increased from 0.30 to 0.40 (more directional)
        - min_bar_range increased from 2.0 to 2.5 (larger bars only)
        - max_bar_range decreased from 6.0 to 5.0 (reject runaway)
        - min_bar_volume increased from 200 to 300 (higher volume)
        - min_tick_count increased from 10 to 12 (more activity)
        - Removed efficiency percentile check (added complexity without benefit)
        
        All changes combine to produce fewer but higher-quality candidate bars.
        """
        if current.volume == 0:
            return None

        bar_range = current.high - current.low

        # Stricter bar range requirement
        if bar_range < self.min_bar_range or bar_range > self.max_bar_range:
            return None

        # Higher volume requirement
        if current.volume < self.min_bar_volume:
            return None

        # Higher tick count requirement
        if current.tick_count < self.min_tick_count:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # STRICTER one-sided flow requirement
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Body must be MORE directional
        bar_body = abs(current.close - current.open)
        body_ratio = bar_body / bar_range if bar_range > 0 else 0
        if body_ratio < self.min_body_ratio:
            return None

        # Check proximity to carried level - much tighter
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Require substantial wick showing price rejection
        if delta > 0:  # Long: buying pressure with lower wick, close near high
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0
            # Wick must be MORE substantial
            if wick_ratio < self.min_wick_ratio:
                return None

            # Close position: must be in top 25% of range (near high)
            close_position = (current.close - current.low) / bar_range if bar_range > 0 else 0
            if close_position < 0.75:
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
        
        elif delta < 0:  # Short: selling pressure with upper wick, close near low
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0
            # Wick must be MORE substantial
            if wick_ratio < self.min_wick_ratio:
                return None

            # Close position: must be in bottom 25% of range (near low)
            close_position = (current.high - current.close) / bar_range if bar_range > 0 else 0
            if close_position < 0.75:
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
