"""
Volume profile absorption strategy - refined signal with cost-aware position management.

Key insights from trial #15 (score=-0.9148, 103,467 trades):
1. Trading frequency of ~270 trades/session is too high - costs dominate
2. Need to reduce trade frequency by 10-20x while maintaining 70%+ exposure floor
3. The absorption hypothesis requires actual absorption detection
4. Multi-session holds are the only way to survive the cost structure

The campaign brief key constraint:
- Must hold for MOVES BETWEEN LEVELS (multi-bar/multi-session holds)
- Cost model (0.2 per round trip) requires 2+ points per trade minimum
- 70%+ exposure floor (270+ sessions) but NOT every bar

New approach - radical frequency reduction:
1. Only trade on strong absorption signals (delta ratio > 75%, wick formation)
2. Price must be at/near a recent level (support/resistance)
3. Much larger targets (5-6 points) to overcome costs on fewer trades
4. Hold positions longer - let them run to targets across multiple bars
5. Only trade ~1-2 times per session on average to reach 270+ sessions naturally
6. Tighter stop (0.5) relative to large target (5.5) for good risk/reward

This reduces from 100k+ trades to ~1-2k trades total, but if each trade
holds for 5-10 points net, the cost structure becomes survivable.
The absorption detection is strict: only when we see actual price rejection.
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback_bars: int = 20,
        absorption_delta_ratio: float = 0.75,
        min_volume: int = 150,
        stop_offset: float = 0.5,
        target_offset: float = 5.5,
        size: int = 1,
        wick_min_ratio: float = 0.20,
        level_proximity: float = 1.0,
    ) -> None:
        self.interval = interval
        self.lookback_bars = lookback_bars
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_volume = min_volume
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.wick_min_ratio = wick_min_ratio
        self.level_proximity = level_proximity

        # Track bars for level identification and absorption detection
        self.bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None
        self.support_levels: deque = deque(maxlen=100)
        self.resistance_levels: deque = deque(maxlen=100)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session

        # Get current bar
        if self.interval not in ctx.bars:
            return

        current_bar = ctx.bars[self.interval]
        self.bars.append(current_bar)

        # Update support/resistance levels from recent bars
        self._update_levels()

        # Skip if already in position - let positions run to target
        if ctx.position or ctx.working_order_ids:
            return

        # Need minimum history to identify levels
        if len(self.bars) < self.lookback_bars + 1:
            return

        # Check for strong absorption signal
        signal = self._detect_absorption(current_bar)

        if signal is None:
            return

        direction, absorption_type = signal

        # Execute trade with large target to overcome costs
        tag = f"absorption-{absorption_type}"

        if direction == Side.BUY:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag=tag,
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_levels(self) -> None:
        """Track support/resistance levels from recent price action."""
        if not self.bars:
            return

        recent = list(self.bars)[-self.lookback_bars :]

        # Find local highs (resistance) - bars where high is a local max
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
        Detect absorption at a level: strong one-way flow with price rejection.
        
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

        # Get previous bar for comparison
        if len(self.bars) < 2:
            return None

        prev_bar = self.bars[-2]
        bar_range = current_bar.high - current_bar.low

        if bar_range < 0.01:
            return None

        # Upside absorption: positive delta (buying pressure) with upper wick (rejection)
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected upside)
            # and not extend significantly above prior high (failed to break)
            if (
                wick_ratio > self.wick_min_ratio
                and current_bar.high <= prev_bar.high + 0.25
            ):
                return (Side.BUY, "upside")

        # Downside absorption: negative delta (selling pressure) with lower wick (rejection)
        elif current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected downside)
            # and not extend significantly below prior low (failed to break)
            if (
                wick_ratio > self.wick_min_ratio
                and current_bar.low >= prev_bar.low - 0.25
            ):
                return (Side.SELL, "downside")

        return None
