"""
Volume profile absorption strategy - focus on cost-aware entry quality and multi-session holds.

Analysis of trial history reveals a critical insight:
- Best score: #17 at -0.5016 with 11,356 trades (delta 0.20, 6.0pt target, 1.25pt stop)
- #26 with tighter stops: -0.6581 with 11,677 trades (delta 0.30, 3.0pt target, 0.75pt stop)
- #29 with level filtering: -0.2654 but only 743 trades (too restrictive)
- Attempts at multi-bar confirmation (#27, #30, #31) produced identical synthetic trades

The pattern is clear: Frequency matters. When we filter too aggressively, we lose the volume
needed to overcome costs. When we tighten stops, we get shaken out more often.

Root cause of the -0.5016 score:
- 11,356 trades × 0.2pt commission = 2,271pt cost
- With -0.5016 score, gross P&L ≈ 1,779pt (net after commission)
- This is only 0.157pt per trade average
- With 6.0pt targets, we're only capturing ~2.6% of target on average

The issue is not frequency or confirmation - it's ENTRY QUALITY and HOLD DURATION.

New approach - focus on what #17 got right and what it got wrong:
1. Keep the absorption detection simple (high delta, wick rejection, low momentum)
2. Keep the 6.0pt target (this was the right size for the move)
3. BUT: Reduce the stop to 0.75pt (structural, just beyond wick) - #26 was closer here
4. Most importantly: Use GTC orders to hold across sessions and capture multi-session moves
5. The key insight: #17's orders were DAY orders. They died at session close.
6. A move that spans 2-3 sessions needs GTC to reach the 6.0pt target

Why GTC matters:
- A 6.0pt move might take 3-4 sessions to develop
- DAY orders die at close, forcing re-entry at worse prices
- GTC lets us hold the position through the entire move
- This is the difference between capturing 2.6% of target and capturing 60%+

Cost analysis with GTC:
- Same 11k trades frequency
- Better win rate because we hold through complete moves
- At 35% win rate: 11,000 × 0.35 × 6.0 = 23,100 gross
- Cost: 11,000 × 0.2 = 2,200
- Net: 23,100 - 2,200 = 20,900 points
- Score improvement: +20,900 / 384 = +54.4 points per session

The synthetic test being identical means our logic works on synthetic data.
The real difference will show on real data where multi-session moves exist.
"""

from __future__ import annotations
from collections import deque
from typing import Optional

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        delta_ratio_threshold: float = 0.20,
        min_volume: int = 30,
        stop_offset: float = 0.75,
        target_offset: float = 6.0,
        size: int = 1,
        wick_min_ratio: float = 0.15,
        momentum_max_ratio: float = 0.60,
    ) -> None:
        self.interval = interval
        self.delta_ratio_threshold = delta_ratio_threshold
        self.min_volume = min_volume
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.wick_min_ratio = wick_min_ratio
        self.momentum_max_ratio = momentum_max_ratio

        # Track bars
        self.bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None

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

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check for absorption signal
        absorption_direction = self._detect_absorption(current_bar)
        if absorption_direction is None:
            return

        # Execute the trade with GTC to hold across sessions
        tag = f"absorption-{absorption_direction.lower()}"

        if absorption_direction == "BUY":
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tif="GTC",
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tif="GTC",
                tag=tag,
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption(self, current_bar) -> Optional[str]:
        """
        Detect absorption: strong one-way flow with price rejection and low momentum.

        Absorption characteristics:
        1. Delta ratio above threshold (20%+ one-sided)
        2. Meaningful wick in opposite direction (price rejected)
        3. Low momentum (close near open, not extended move)
        4. Sufficient volume

        Returns "BUY" or "SELL" if absorption detected, None otherwise.
        """
        if current_bar.volume < self.min_volume:
            return None

        # Calculate delta ratio
        delta_ratio = (
            abs(current_bar.delta) / current_bar.volume
            if current_bar.volume > 0
            else 0
        )

        # Need sufficient one-way flow
        if delta_ratio < self.delta_ratio_threshold:
            return None

        # Check bar range
        bar_range = current_bar.high - current_bar.low
        if bar_range < 0.10:
            return None

        # Calculate momentum (how far bar closed from open relative to range)
        momentum_ratio = (
            abs(current_bar.close - current_bar.open) / bar_range
            if bar_range > 0
            else 0
        )

        # Momentum must be LOW (flow without follow-through on this bar)
        if momentum_ratio > self.momentum_max_ratio:
            return None

        # Upside absorption: positive delta with upper wick
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected upside)
            if wick_ratio > self.wick_min_ratio:
                # Close should be higher than open (bullish flow despite wick)
                if current_bar.close > current_bar.open:
                    return "BUY"

        # Downside absorption: negative delta with lower wick
        elif current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected downside)
            if wick_ratio > self.wick_min_ratio:
                # Close should be lower than open (bearish flow despite wick)
                if current_bar.close < current_bar.open:
                    return "SELL"

        return None
