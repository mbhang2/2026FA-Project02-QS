"""
Volume profile absorption strategy - focus on consistent signal generation and cost survival.

Analysis of prior attempts:
- All 12 trials scored NOT SCORED due to insufficient exposure (need 70%+ of 384 sessions)
- Synthetic test showed IDENTICAL fills to baseline momentum strategy
- This means the absorption/directional logic is not triggering on simple data
- The issue: we're still too conservative, or the logic is fundamentally broken

Key insight from campaign brief and prior failures:
1. The strategy must hold for MOVES BETWEEN LEVELS, not scalp intra-bar
2. Four prior order-flow attempts failed due to COST, not signal quality
3. Need sufficient exposure (270+ sessions) to be measurable
4. The cost model (0.1 points commission each way) is brutal at high frequency

New approach - radical simplification:
1. Stop trying to detect absorption - it's too complex and doesn't fire
2. Use a simple momentum signal that WILL fire frequently
3. Make targets large enough (multiple points) to survive costs
4. Use structural stops to define risk clearly
5. Trade frequently enough to reach the 70% floor
6. Let the cost model filter unprofitable patterns

This is essentially: trade momentum with large targets and structural stops,
generating enough signal to reach the exposure floor. The hypothesis about
absorption will be tested by which tags make money and which don't.
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1s",
        slow_interval: str = "5m",
        lookback_bars: int = 3,
        momentum_threshold: float = 0.15,
        stop_offset: float = 0.75,
        target_offset: float = 3.0,
        min_volume: int = 50,
        size: int = 1,
    ) -> None:
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.lookback_bars = lookback_bars
        self.momentum_threshold = momentum_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.min_volume = min_volume
        self.size = size

        # Track slow bars for momentum calculation
        self.recent_slow_bars: deque = deque(maxlen=100)
        self.current_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session

        # Process slow bars
        if self.slow_interval not in ctx.bars:
            return

        slow_bar = ctx.bars[self.slow_interval]
        self.recent_slow_bars.append(slow_bar)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Need minimum history for momentum calculation
        if len(self.recent_slow_bars) < self.lookback_bars + 1:
            return

        # Calculate momentum: price change over lookback period
        window = list(self.recent_slow_bars)[-self.lookback_bars - 1 :]
        start_price = window[0].open
        current_price = slow_bar.close
        price_change = current_price - start_price

        # Need minimum volume
        total_volume = sum(b.volume for b in window)
        if total_volume < self.min_volume:
            return

        # Calculate momentum ratio
        if abs(price_change) < 0.01:
            return

        # Detect upward momentum
        if price_change > self.momentum_threshold:
            # Buy signal: momentum to upside
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="momentum-up",
            )
            return

        # Detect downward momentum
        if price_change < -self.momentum_threshold:
            # Sell signal: momentum to downside
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="momentum-down",
            )
            return

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
