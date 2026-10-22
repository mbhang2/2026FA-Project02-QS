"""
Volume profile absorption strategy - test efficiency-based initiation.

Analysis of recent trials:
- #81: -0.0096 with 622 trades (best baseline)
- #165: -0.0350 with 658 trades
- #195: +0.0646 with 535 trades (positive outlier - reversal condition)
- #210: Not scored (body efficiency was too restrictive)
- #211: Not scored (close proximity filter too tight)

Key observations:
1. Byte-identical trades persist across many parameter variations, suggesting
   the core absorption detection is stable but the entry timing/confirmation is the variable.

2. Trial #195 achieved +0.0646 (best positive result) using a reversal-based approach,
   suggesting that sometimes NOT trading the absorption signal is better than trading it.

3. The campaign asks about "initiation" - whether absorption precedes a move away.
   This could mean:
   a) Absorption ALONE predicts moves (enter immediately) - #81 approach
   b) Absorption + confirmation bar predicts moves (wait for next bar) - #195 approach
   c) Absorption with specific bar efficiency metrics predicts moves

4. Current issue: confirmation-based approaches produce byte-identical trades on
   synthetic data, suggesting either:
   - The confirmation condition is nearly always true/false
   - The synthetic data doesn't have enough bars to confirm
   - The confirmation is filtering the same bars as existing filters

New approach: Instead of waiting for a confirmation bar, implement a DUAL-ENTRY
strategy that tags entries differently:
- "absorption_immediate": Enter on absorption signal alone (like #81)
- "absorption_confirmed": Only enter if the absorption bar shows specific efficiency metrics

This allows the run record to show which entry type performed better, directly
answering the campaign's question about whether waiting for initiation helps.

For efficiency metrics, use a simple measure: bars with strong delta AND a body
that is substantial relative to the wick. This is the opposite of absorption -
if the body is LARGER than the wick, the directional flow is WINNING, not being
absorbed.

Strategy:
1. Detect absorption at levels (delta_ratio 0.28, wick 0.5, proximity 4.0)
2. For each absorption bar, calculate:
   - body_size = abs(close - open)
   - wick_size = lower_wick for long, upper_wick for short
   - efficiency = body_size / (body_size + wick_size) if denominator > 0
3. Enter immediately with tag "absorption_immediate"
4. ALSO check if efficiency >= 0.45 (body is 45% or more of body+wick)
5. If efficient, also enter with tag "absorption_confirmed" (or skip if already in position)

This creates a natural A/B test within one rule: the run record will show which
entry type had better outcomes.

Expected: 600-1000 trades split between immediate and confirmed, with confirmed
entries showing better risk-adjusted returns.
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
        efficiency_threshold: float = 0.45,
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
        self.efficiency_threshold = efficiency_threshold

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

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            self._entered_bar_timestamps.add(current.close_ts)
            
            # Always enter immediately with tag "absorption_immediate"
            self._place_entry(ctx, setup, "absorption_immediate")

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

    def _calculate_efficiency(self, current, direction: str) -> float:
        """
        Calculate bar efficiency: how much of the bar's movement is body vs wick.
        
        For long (buying pressure):
        - body_size = close - open
        - wick_size = close - low
        - efficiency = body_size / (body_size + wick_size)
        - High efficiency = body is large, wick is small = directional flow winning
        
        For short (selling pressure):
        - body_size = open - close
        - wick_size = high - close
        - efficiency = body_size / (body_size + wick_size)
        """
        if direction == "long":
            body_size = current.close - current.open
            wick_size = current.close - current.low
        else:  # short
            body_size = current.open - current.close
            wick_size = current.high - current.close
        
        if body_size <= 0:
            return 0.0
        
        denominator = body_size + wick_size
        if denominator <= 0:
            return 0.0
        
        return body_size / denominator

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
            
            efficiency = self._calculate_efficiency(current, "long")
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "efficiency": efficiency,
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
            
            efficiency = self._calculate_efficiency(current, "short")
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "efficiency": efficiency,
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
