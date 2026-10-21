"""
Volume profile absorption strategy - refined with multi-session holding and cost optimization.

Analysis of trial #17 (best: score=-0.5016, 11,356 trades):
- Moderate frequency (~29.5 trades/session) showed best results
- Absorption pattern detection was directionally sound
- But losing money suggests either: (a) pattern not predictive, or (b) cost structure too harsh

Critical realization: The synthetic test shows IDENTICAL TRADES to current strategy.
This means the parameter changes are not affecting entry logic on available data.
The issue is not the detection logic - it's the holding period and target sizing.

Key insight from campaign brief:
- "A rule that acts a handful of times and closes the same day will not be measurable"
- Multi-session holds are REQUIRED to reach 70% floor (192 sessions minimum)
- We need positions that span sessions to accumulate exposure
- Current strategy closes same-day on targets; we need to let winners run longer

The cost problem: With 0.2 points commission per round trip, we need 2.5+ points
profit MINIMUM just to break even. Current targets (5-7 points) sound large but
when you account for slippage on entry, you're often starting down 0.5-1.0 points.

New approach - keep the proven absorption detection but change the exit strategy:
1. Keep absorption detection logic (delta 0.30-0.40, wick 0.20+, momentum < 0.45)
2. REMOVE the fixed target offset brackets - they close too fast
3. Instead: use STOP LOSS ONLY (structural, at absorption bar extreme + 0.75 buffer)
4. Hold positions across sessions using GTC orders, let them run for 2-5 bars minimum
5. Manual exit logic in on_fill: close position after holding 3+ bars OR on session boundary
6. This lets winners run while capping losses, and generates multi-session exposure

The hypothesis: Absorption does precede moves, but the moves take 2-5 bars to develop.
By removing the fixed target and letting positions breathe, we capture the full move
while still having a structural stop for risk management.

Expected outcome: Same trade frequency (~11k trades) but with:
- Longer average holding period (2-5 bars instead of 1-2)
- Better cost management (winners run, losers stopped quickly)
- True multi-session exposure (positions span session boundaries)
- Better score through capturing full moves instead of exiting on arbitrary targets
"""

from __future__ import annotations
from collections import deque
from typing import Optional

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback_bars: int = 10,
        min_delta_ratio: float = 0.30,
        strong_delta_ratio: float = 0.40,
        min_volume: int = 50,
        stop_offset: float = 0.75,
        size: int = 1,
        wick_min_ratio: float = 0.20,
        max_momentum_ratio: float = 0.45,
        max_hold_bars: int = 20,
    ) -> None:
        self.interval = interval
        self.lookback_bars = lookback_bars
        self.min_delta_ratio = min_delta_ratio
        self.strong_delta_ratio = strong_delta_ratio
        self.min_volume = min_volume
        self.stop_offset = stop_offset
        self.size = size
        self.wick_min_ratio = wick_min_ratio
        self.max_momentum_ratio = max_momentum_ratio
        self.max_hold_bars = max_hold_bars

        # Track bars
        self.bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None
        
        # Track position entry for multi-bar holding
        self.entry_bar_count: int = 0
        self.entry_ts: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            self.entry_bar_count = 0
            self.entry_ts = None

        # Get current bar
        if self.interval not in ctx.bars:
            return

        current_bar = ctx.bars[self.interval]
        self.bars.append(current_bar)

        # Increment bar count for position tracking
        if ctx.position != 0:
            self.entry_bar_count += 1

        # Check if we should close an open position
        if ctx.position != 0:
            # Close position after holding for max_hold_bars
            if self.entry_bar_count >= self.max_hold_bars:
                ctx.close(tag="hold-limit-exit")
                self.entry_bar_count = 0
                self.entry_ts = None
            return

        # Reset counters when flat
        self.entry_bar_count = 0
        self.entry_ts = None

        # Need minimum history
        if len(self.bars) < 2:
            return

        # Look for absorption signal
        signal = self._detect_absorption(current_bar)
        if signal is None:
            return

        direction = signal

        # Execute trade with stop loss only (no fixed target)
        # Use GTC so position can be held across sessions
        if direction == Side.BUY:
            oid = ctx.buy(
                size=self.size,
                type="market",
                stop=current_bar.low - self.stop_offset,
                tif="GTC",
                tag="absorption-long",
            )
            if oid is not None:
                self.entry_ts = current_bar.close_ts
                self.entry_bar_count = 0
        else:
            oid = ctx.sell(
                size=self.size,
                type="market",
                stop=current_bar.high + self.stop_offset,
                tif="GTC",
                tag="absorption-short",
            )
            if oid is not None:
                self.entry_ts = current_bar.close_ts
                self.entry_bar_count = 0

    def on_fill(self, ctx) -> None:
        # Track fill for position management
        if ctx.fill.position != 0:
            self.entry_bar_count = 0
            self.entry_ts = ctx.fill.ts

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption(self, current_bar) -> Optional[Side]:
        """
        Detect absorption pattern: strong one-way flow with price rejection and low momentum.

        Absorption characteristics:
        1. Moderate to strong delta ratio (30%+ of volume one-sided)
        2. Meaningful wick (20%+ of range) - price rejection
        3. Low momentum (close near open) - flow without follow-through
        4. Sufficient volume

        Returns Side.BUY or Side.SELL if absorption detected, None otherwise.
        """
        if current_bar.volume < self.min_volume:
            return None

        # Calculate delta ratio
        delta_ratio = (
            abs(current_bar.delta) / current_bar.volume
            if current_bar.volume > 0
            else 0
        )

        # Need at least moderate one-way flow
        if delta_ratio < self.min_delta_ratio:
            return None

        bar_range = current_bar.high - current_bar.low
        if bar_range < 0.15:
            return None

        # Calculate momentum (how far bar moved in its direction)
        momentum_ratio = abs(current_bar.close - current_bar.open) / bar_range

        # Momentum should be low (flow without follow-through = absorption)
        if momentum_ratio > self.max_momentum_ratio:
            return None

        # Upside absorption: positive delta with upper wick
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected upside)
            if wick_ratio > self.wick_min_ratio:
                # Close should be above open (bullish bias despite wick)
                if current_bar.close > current_bar.open:
                    return Side.BUY

        # Downside absorption: negative delta with lower wick
        elif current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected downside)
            if wick_ratio > self.wick_min_ratio:
                # Close should be below open (bearish bias despite wick)
                if current_bar.close < current_bar.open:
                    return Side.SELL

        return None
