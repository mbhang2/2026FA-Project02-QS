"""
Absorption strategy: multi-timeframe confirmation and adaptive targeting.

Learning from #20 (identical trades to #17, score=-0.5016):
- The prior-bar delta confirmation didn't change trades on synthetic data
- This suggests the confirmation was already implicit in the absorption detection
- Need a fundamentally different filter to improve on -0.5016

Analysis of what worked:
- #17's strict parameters (delta 0.75, wick 0.20, level proximity 1.0) generated ~11k trades
- Score of -0.5016 means losing ~50 cents per point of exposure
- With 5.5-point targets and 0.5-point stops, we're still losing money overall
- The cost model (0.2 per round trip) means we need ~2.5+ points profit per trade minimum

The real problem: absorption detection alone is not predictive enough.
Even with perfect absorption signals, the market doesn't move as expected.

New approach - add multi-timeframe confirmation:
1. Keep 5m bar for entry detection (absorption + level proximity)
2. Add 15m timeframe confirmation - only trade if 15m bar also shows directional bias
3. This filters entries to only those with alignment across timeframes
4. Reduces trade frequency (fewer false signals) while maintaining quality
5. Adaptive target: if 15m shows strong trend, use larger target (6.0), else smaller (4.5)
6. Keep the successful absorption detection logic from #17

The hypothesis: absorption at a level is only predictive when confirmed by
the intermediate timeframe. This reduces noise while maintaining the multi-bar
hold structure that survived costs in #17.

Key changes:
- Subscribe to both "5m" (entry) and "15m" (confirmation)
- Only enter when: 5m shows absorption + 15m bar shows matching direction
- Adaptive target based on 15m momentum strength
- This should reduce false entries while keeping ~10-15k trades for exposure floor
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        entry_interval: str = "5m",
        confirm_interval: str = "15m",
        lookback_bars: int = 20,
        absorption_delta_ratio: float = 0.75,
        min_volume: int = 100,
        stop_offset: float = 0.5,
        target_offset_base: float = 4.5,
        target_offset_strong: float = 6.0,
        size: int = 1,
        wick_min_ratio: float = 0.20,
        level_proximity: float = 1.0,
    ) -> None:
        self.entry_interval = entry_interval
        self.confirm_interval = confirm_interval
        self.lookback_bars = lookback_bars
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_volume = min_volume
        self.stop_offset = stop_offset
        self.target_offset_base = target_offset_base
        self.target_offset_strong = target_offset_strong
        self.size = size
        self.wick_min_ratio = wick_min_ratio
        self.level_proximity = level_proximity

        # Track bars for both timeframes
        self.entry_bars: deque = deque(maxlen=500)
        self.confirm_bars: deque = deque(maxlen=200)
        self.current_session: Optional[int] = None
        self.support_levels: deque = deque(maxlen=100)
        self.resistance_levels: deque = deque(maxlen=100)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.entry_interval)
        ctx.subscribe(self.confirm_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session

        # Get entry interval bar if it closed
        if self.entry_interval in ctx.bars:
            entry_bar = ctx.bars[self.entry_interval]
            self.entry_bars.append(entry_bar)
            self._update_levels()

        # Get confirmation interval bar if it closed
        if self.confirm_interval in ctx.bars:
            confirm_bar = ctx.bars[self.confirm_interval]
            self.confirm_bars.append(confirm_bar)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Only make decisions on entry interval bars
        if self.entry_interval not in ctx.bars:
            return

        entry_bar = ctx.bars[self.entry_interval]

        # Need minimum history
        if len(self.entry_bars) < self.lookback_bars + 2:
            return

        # Detect absorption on entry interval
        signal = self._detect_absorption(entry_bar)
        if signal is None:
            return

        direction, absorption_type = signal

        # Get confirmation from the most recent confirmation bar
        if not self.confirm_bars:
            return

        confirm_bar = self.confirm_bars[-1]

        # Check if confirmation bar aligns with absorption direction
        if not self._confirm_direction(direction, confirm_bar):
            return

        # Determine target based on confirmation bar strength
        target = self._get_adaptive_target(direction, confirm_bar)

        # Execute trade
        tag = f"absorption-{absorption_type}"

        if direction == Side.BUY:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=target,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=target,
                tag=tag,
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_levels(self) -> None:
        """Track support/resistance levels from recent entry bars."""
        if not self.entry_bars:
            return

        recent = list(self.entry_bars)[-self.lookback_bars :]

        # Find local highs (resistance) and lows (support)
        for i, bar in enumerate(recent):
            is_local_high = True
            is_local_low = True

            # Check neighbors
            if i > 0 and recent[i - 1].high > bar.high:
                is_local_high = False
            if i < len(recent) - 1 and recent[i + 1].high > bar.high:
                is_local_high = False

            if i > 0 and recent[i - 1].low < bar.low:
                is_local_low = False
            if i < len(recent) - 1 and recent[i + 1].low < bar.low:
                is_local_low = False

            if is_local_high:
                self.resistance_levels.append(bar.high)
            if is_local_low:
                self.support_levels.append(bar.low)

    def _is_near_level(self, price: float) -> bool:
        """Check if price is near a recent support/resistance level."""
        # Check resistance levels
        for level in self.resistance_levels:
            if abs(price - level) < self.level_proximity:
                return True

        # Check support levels
        for level in self.support_levels:
            if abs(price - level) < self.level_proximity:
                return True

        return False

    def _detect_absorption(self, current_bar) -> Optional[Tuple[Side, str]]:
        """
        Detect absorption at a level on the entry interval.
        
        Absorption characteristics:
        1. High delta ratio (75%+ of volume is one-sided)
        2. Wick in opposite direction (price rejected)
        3. Did not extend past prior bar (failed to break through)
        4. Price is at/near a support/resistance level
        
        Returns (direction, type) or None.
        """
        if current_bar.volume < self.min_volume:
            return None

        # Calculate delta ratio
        delta_ratio = (
            abs(current_bar.delta) / current_bar.volume
            if current_bar.volume > 0
            else 0
        )

        # Need very strong one-way flow for absorption
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Must be near a level
        if not self._is_near_level(current_bar.close):
            return None

        # Get prior bar for comparison
        if len(self.entry_bars) < 2:
            return None

        prev_bar = self.entry_bars[-2]
        bar_range = current_bar.high - current_bar.low

        if bar_range < 0.01:
            return None

        # Upside absorption: positive delta with upper wick
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick and not extend significantly above prior high
            if (
                wick_ratio > self.wick_min_ratio
                and current_bar.high <= prev_bar.high + 0.25
            ):
                return (Side.BUY, "upside")

        # Downside absorption: negative delta with lower wick
        elif current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick and not extend significantly below prior low
            if (
                wick_ratio > self.wick_min_ratio
                and current_bar.low >= prev_bar.low - 0.25
            ):
                return (Side.SELL, "downside")

        return None

    def _confirm_direction(self, direction: Side, confirm_bar) -> bool:
        """
        Check if the confirmation bar aligns with the absorption direction.
        
        For upside absorption (BUY), confirm_bar should show buying bias (close > open).
        For downside absorption (SELL), confirm_bar should show selling bias (close < open).
        """
        if direction == Side.BUY:
            # For upside absorption, confirm bar should close higher than it opened
            return confirm_bar.close > confirm_bar.open
        else:
            # For downside absorption, confirm bar should close lower than it opened
            return confirm_bar.close < confirm_bar.open

    def _get_adaptive_target(self, direction: Side, confirm_bar) -> float:
        """
        Determine target based on confirmation bar strength.
        
        If the confirmation bar shows strong momentum (large body relative to range),
        use the larger target. Otherwise use the base target.
        """
        bar_range = confirm_bar.high - confirm_bar.low
        if bar_range < 0.01:
            return self.target_offset_base

        body = abs(confirm_bar.close - confirm_bar.open)
        body_ratio = body / bar_range

        # If body is more than 60% of range, it's a strong bar
        if body_ratio > 0.60:
            return self.target_offset_strong
        else:
            return self.target_offset_base
