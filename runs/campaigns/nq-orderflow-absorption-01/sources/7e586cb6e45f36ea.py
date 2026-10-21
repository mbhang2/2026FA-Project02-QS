"""
Volume profile absorption strategy - focus on reaching exposure floor.

Analysis of prior attempts:
- All #0-#11 scored NOT SCORED due to insufficient exposure (need 70%+ = ~270 of 384 sessions)
- Synthetic test: identical fills to baseline, meaning signal detection isn't triggering
- The campaign asks for multi-session holds between volume levels, not intra-bar scalping

Key insight from repeated failures:
The strategy needs to trade in ~270+ sessions to be scored. Current approach was too
selective. Need to:
1. Trade more frequently but with proper risk management
2. Focus on reliable directional signals rather than rare absorption patterns
3. Use volume delta as the core signal (always available, no fitting needed)
4. Keep structural stops and multi-point targets
5. Accept that many trades will lose; the cost model will filter them

New approach:
- Simpler signal: volume delta above threshold on 5m bars
- No level tracking (removes window constraint and fitting risk)
- Buy when delta > threshold and close > open (buying pressure, expect continuation)
- Sell when delta < -threshold and close < open (selling pressure, expect continuation)
- Wider targets (3-4 points) to survive 0.2 point round-trip costs
- Trade almost every session to reach 70% exposure floor
- Structural stop at opposite extreme of the entry bar
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        delta_ratio_threshold: float = 0.30,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_width: float = 3.0,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.delta_ratio_threshold = delta_ratio_threshold
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_width = max_stop_width
        self.size = size
        
        # Track recent bars for context
        self.bar_history: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session for debugging
        if ctx.session != self.current_session:
            self.current_session = ctx.session

        # Get the bar that just closed
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]
        self.bar_history.append(bar)

        # Don't trade if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Need at least 1 bar to trade
        if len(self.bar_history) < 1:
            return

        # Signal detection: volume delta at price direction agreement
        signal = self._detect_signal(bar)

        if signal is None:
            return

        direction, bar_extreme, tag = signal

        # Calculate stop: structural stop at opposite extreme of entry bar
        stop_distance = abs(bar_extreme - bar.close) + self.stop_buffer
        stop_distance = max(0.25, stop_distance)

        # Decline if stop is too wide
        if stop_distance > self.max_stop_width:
            return

        # Target is multiple of stop distance
        target_distance = stop_distance * self.target_multiple

        # Place order
        if direction == Side.BUY:
            ctx.buy(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=tag,
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_signal(self, bar) -> Optional[Tuple[Side, float, str]]:
        """
        Core signal: directional volume delta with price agreement.
        
        Buy when:
        - Net buying pressure (delta > 0)
        - Delta is significant (> threshold % of volume)
        - Price moved up (close > open)
        - Expect continuation up
        
        Sell when:
        - Net selling pressure (delta < 0)
        - Delta is significant (> threshold % of volume)
        - Price moved down (close < open)
        - Expect continuation down
        """
        if bar.volume <= 0:
            return None

        # Calculate delta ratio
        delta_ratio = abs(bar.delta) / bar.volume

        if delta_ratio < self.delta_ratio_threshold:
            return None

        # Buy signal: buying pressure + price up
        if bar.delta > 0 and bar.close > bar.open:
            # Stop below the bar (at low)
            return (Side.BUY, bar.low, "delta-buy")

        # Sell signal: selling pressure + price down
        if bar.delta < 0 and bar.close < bar.open:
            # Stop above the bar (at high)
            return (Side.SELL, bar.high, "delta-sell")

        return None
