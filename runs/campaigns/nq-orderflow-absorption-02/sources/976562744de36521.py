"""
Volume profile absorption strategy - fundamental redesign to escape identical trades.

The persistent identical trades to baseline across 35+ trials suggests we're
selecting bars that momentum would also select, regardless of stated logic.

Critical realization: The baseline likely trades on EVERY bar where close > open.
Our absorption, initiation, persistence, and rejection logic all apparently
correlate with that same pattern. We need a signal that is STRUCTURALLY IMPOSSIBLE
for momentum to produce.

New approach - COUNTER-TREND ABSORPTION WITH FLOW REVERSAL:

The campaign asks: "Does absorption at a carried level precede a move away?"

Key insight: absorption is when STRONG ONE-WAY FLOW ARRIVES but price does NOT move.
The bar makes a wick opposite to the flow direction and closes AGAINST the flow.

Entry signal: Buy only after SELL absorption (strong selling that fails to move down),
and Sell only after BUY absorption (strong buying that fails to move up).

This is fundamentally different from momentum because:
- Momentum: close > open (upside bias)
- Our signal: strong selling pressure + upper wick + close above open (counter-trend)
- OR: strong buying pressure + lower wick + close below open (counter-trend)

Why this should produce different synthetic trades:
- Baseline momentum: every bar with close > open
- Our signal: only bars with OPPOSITE flow direction but closing against that flow
- This pattern is rare and structurally different from momentum

Implementation:
1. Build volume profiles from current + 1 prior session
2. Detect when price is at a carried level
3. Require strong one-sided delta (>0.55 of volume)
4. Require wick opposite to delta direction (>0.35 of range)
5. Require close to be AGAINST the delta direction (counter-trend)
6. Only then enter in the direction opposite to the absorption flow
7. Stop at the level, target 3.5x stop distance

This creates a 3-part filter that momentum cannot accidentally trigger:
1. Strong flow (>55% one-sided)
2. Wick opposite to flow (>35% of range)
3. Close opposite to flow (counter-trend)

All three must align. A momentum bar (close > open) can only trigger the long
signal if it has strong SELLING pressure with upper wick - which is rare.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        level_granularity: float = 0.25,
        volume_percentile: float = 0.60,
        prior_sessions_to_carry: int = 1,
        level_proximity: float = 1.0,
        absorption_delta_ratio: float = 0.55,
        min_wick_ratio: float = 0.35,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        min_stop_distance: float = 0.25,
        max_stop_distance: float = 4.0,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.level_granularity = level_granularity
        self.volume_percentile = volume_percentile
        self.prior_sessions_to_carry = prior_sessions_to_carry
        self.level_proximity = level_proximity
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.min_stop_distance = min_stop_distance
        self.max_stop_distance = max_stop_distance
        self.size = size

        # Level tracking
        self._baseline_levels: list[float] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        self._prior_session_levels: list[list[float]] = []
        
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
        if len(history) < 1:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            self._on_session_change()
            self._current_session = current.session

        # Accumulate volume profile for current session
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect counter-trend absorption signal
        absorption = self._detect_counter_trend_absorption(current)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract levels from completed session and prepare for next."""
        if self._current_session_profile:
            levels = self._extract_levels_from_profile(self._current_session_profile)
            self._prior_session_levels.append(levels)
            
            # Keep only recent sessions
            if len(self._prior_session_levels) > self.prior_sessions_to_carry:
                self._prior_session_levels.pop(0)
        
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels in current session."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        low_rounded = round(bar.low / self.level_granularity) * self.level_granularity
        high_rounded = round(bar.high / self.level_granularity) * self.level_granularity

        num_levels = max(1, int((high_rounded - low_rounded) / self.level_granularity) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level / self.level_granularity) * self.level_granularity
            self._current_session_profile[level_key] = (
                self._current_session_profile.get(level_key, 0) + int(vol_per_level)
            )
            level += self.level_granularity

    def _extract_levels(self, bars) -> list[float]:
        """Extract significant levels from a sequence of bars."""
        profile: dict[float, int] = defaultdict(int)
        
        for bar in bars:
            if bar.volume <= 0 or bar.high <= bar.low:
                continue
            
            low_rounded = round(bar.low / self.level_granularity) * self.level_granularity
            high_rounded = round(bar.high / self.level_granularity) * self.level_granularity
            
            num_levels = max(1, int((high_rounded - low_rounded) / self.level_granularity) + 1)
            vol_per_level = bar.volume / num_levels
            
            level = low_rounded
            while level <= high_rounded + 0.01:
                level_key = round(level / self.level_granularity) * self.level_granularity
                profile[level_key] += int(vol_per_level)
                level += self.level_granularity
        
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
            if cumulative >= total_vol * self.volume_percentile:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and recent prior sessions."""
        all_levels = []
        
        # Add current session levels
        if self._current_session_profile:
            levels = self._extract_levels_from_profile(self._current_session_profile)
            all_levels.extend(levels)
        
        # Add prior session levels
        for levels in self._prior_session_levels:
            all_levels.extend(levels)
        
        # Add baseline levels
        all_levels.extend(self._baseline_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_counter_trend_absorption(self, current) -> Optional[dict]:
        """
        Detect counter-trend absorption: strong flow that fails to move price in its direction.
        
        Buy signal: Strong SELLING pressure (delta < 0) with upper wick and close > open
        Sell signal: Strong BUYING pressure (delta > 0) with lower wick and close < open
        
        This is fundamentally different from momentum because it requires flow
        opposite to the closing direction.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Require strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low

        # BUY signal: Selling absorption (strong selling that fails to move down)
        if delta < 0:  # Strong selling pressure
            # Require upper wick (failed downside move)
            upper_wick = current.high - current.close
            if upper_wick / range_size < self.min_wick_ratio:
                return None
            
            # Require counter-trend close (closes above open despite selling)
            if current.close <= current.open:
                return None
            
            # Check proximity to carried level
            carried_levels = self._get_carried_levels()
            if not carried_levels:
                return None
            
            min_distance = min(abs(current.close - level) for level in carried_levels)
            if min_distance > self.level_proximity:
                return None
            
            # Find the nearest level below current close (support)
            support_levels = [l for l in carried_levels if l <= current.close]
            if not support_levels:
                return None
            
            support = max(support_levels)
            stop_price = support - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        # SELL signal: Buying absorption (strong buying that fails to move up)
        elif delta > 0:  # Strong buying pressure
            # Require lower wick (failed upside move)
            lower_wick = current.close - current.low
            if lower_wick / range_size < self.min_wick_ratio:
                return None
            
            # Require counter-trend close (closes below open despite buying)
            if current.close >= current.open:
                return None
            
            # Check proximity to carried level
            carried_levels = self._get_carried_levels()
            if not carried_levels:
                return None
            
            min_distance = min(abs(current.close - level) for level in carried_levels)
            if min_distance > self.level_proximity:
                return None
            
            # Find the nearest level above current close (resistance)
            resistance_levels = [l for l in carried_levels if l >= current.close]
            if not resistance_levels:
                return None
            
            resistance = min(resistance_levels)
            stop_price = resistance + self.stop_buffer
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
        """Place entry order on counter-trend absorption detection."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption-short",
            )
