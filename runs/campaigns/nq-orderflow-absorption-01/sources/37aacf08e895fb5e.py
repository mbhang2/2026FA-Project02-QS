"""
Volume profile absorption strategy - refined for real market conditions.

The campaign asks: does absorption at a carried volume level precede a move
away from it, large enough to reach the next level?

Prior trials show the core challenge: generating enough signal (>50% exposure)
while keeping absorption detection meaningful. Trials #6 and #7 had identical
trades to baseline, suggesting the absorption conditions weren't triggering.

This revision focuses on:
1. Simpler, more reliable level identification: track VPOC directly from 1s bars
2. Relaxed but structured absorption detection: high delta + visible rejection
3. More lenient level proximity matching to catch price arriving "at" levels
4. Proper multi-session holding with structural stops
5. Optional confirmation bar to test if waiting improves outcomes

Key insight from prior failures: the synthetic data may have different
characteristics than real NQ. This version is more adaptive to what the data
actually shows rather than what we expect.
"""

from __future__ import annotations
from collections import defaultdict, deque
from typing import Dict, Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1s",
        slow_interval: str = "5m",
        sessions_to_carry: int = 2,
        absorption_delta_ratio: float = 0.50,
        level_proximity: float = 3.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.0,
        max_stop_width: float = 4.0,
        use_confirmation: bool = False,
        min_body_ratio: float = 0.5,
        size: int = 1,
    ) -> None:
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.sessions_to_carry = sessions_to_carry
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_width = max_stop_width
        self.use_confirmation = use_confirmation
        self.min_body_ratio = min_body_ratio
        self.size = size

        # Accumulated state across window boundaries
        self.current_session: Optional[int] = None
        self.session_vpoc: Dict[int, float] = {}
        self.session_price_volume: Dict[int, Dict[float, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.session_total_volume: Dict[int, float] = defaultdict(float)

        # Track bars for absorption detection and confirmation
        self.slow_bar_history: deque = deque(maxlen=100)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session boundaries
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            # Clean up old sessions
            sessions_to_keep = set(
                range(max(0, ctx.session - self.sessions_to_carry), ctx.session + 1)
            )
            self.session_vpoc = {
                s: v for s, v in self.session_vpoc.items() if s in sessions_to_keep
            }
            self.session_price_volume = defaultdict(
                lambda: defaultdict(float),
                {
                    s: v
                    for s, v in self.session_price_volume.items()
                    if s in sessions_to_keep
                },
            )
            self.session_total_volume = {
                s: v
                for s, v in self.session_total_volume.items()
                if s in sessions_to_keep
            }

        # Accumulate volume profile from fast bars
        if self.fast_interval in ctx.bars:
            fast_bar = ctx.bars[self.fast_interval]
            self._accumulate_volume(ctx.session, fast_bar)

        # Decision logic on slow bars
        if self.slow_interval not in ctx.bars:
            return

        slow_bar = ctx.bars[self.slow_interval]
        self.slow_bar_history.append(slow_bar)

        # Don't trade if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Need at least 2 bars for comparison
        if len(self.slow_bar_history) < 2:
            return

        prev_slow = self.slow_bar_history[-2]
        curr_slow = slow_bar

        # Detect absorption at carried levels
        signal = self._detect_absorption(curr_slow, prev_slow, ctx.session)
        if signal is None:
            return

        level_price, direction, bar_extreme = signal

        # Calculate stop distance from absorption bar's extreme
        stop_distance = abs(bar_extreme - level_price) + self.stop_buffer

        # Decline if stop is too wide
        if stop_distance > self.max_stop_width:
            return

        target_distance = stop_distance * self.target_multiple

        # Optional confirmation: wait for initiation bar
        tag = "absorption-only"
        if self.use_confirmation:
            if not self._check_initiation(curr_slow):
                return
            tag = "absorption-confirmed"

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

    def _accumulate_volume(self, session: int, bar) -> None:
        """Build volume profile from fast bars."""
        if bar.volume <= 0 or bar.tick_count == 0:
            return

        # Distribute volume across price levels (0.25 point bins)
        tick_size = 0.25
        num_levels = max(1, int((bar.high - bar.low) / tick_size) + 1)
        vol_per_level = bar.volume / num_levels

        price = bar.low
        while price <= bar.high + tick_size * 0.01:
            price_key = round(price / tick_size, 2) * tick_size
            price_key = round(price_key, 2)
            self.session_price_volume[session][price_key] += vol_per_level
            self.session_total_volume[session] += vol_per_level
            price += tick_size

        # Update VPOC
        if self.session_price_volume[session]:
            vpoc = max(self.session_price_volume[session].items(), key=lambda x: x[1])[
                0
            ]
            self.session_vpoc[session] = vpoc

    def _get_carried_levels(self, current_session: int) -> Dict[float, int]:
        """Get VPOC levels from current and prior sessions."""
        levels: Dict[float, int] = {}

        for offset in range(self.sessions_to_carry + 1):
            session = current_session - offset
            if session < 0:
                continue

            vpoc = self.session_vpoc.get(session)
            if vpoc is not None and vpoc not in levels:
                levels[vpoc] = offset

        return levels

    def _detect_absorption(
        self, bar, prev_bar, session: int
    ) -> Optional[Tuple[float, Side, float]]:
        """
        Detect absorption: price arrives at a carried level with high one-way
        delta and shows rejection (tail) without extending prior extreme.

        Returns: (level_price, direction, bar_extreme) or None
        """
        carried_levels = self._get_carried_levels(session)
        if not carried_levels:
            return None

        # Find nearby carried level
        level_at_bar: Optional[float] = None
        for level in carried_levels.keys():
            if abs(bar.close - level) <= self.level_proximity:
                level_at_bar = level
                break

        if level_at_bar is None:
            return None

        # Require sufficient one-way delta
        if bar.volume <= 0:
            return None

        delta_ratio = abs(bar.delta) / bar.volume
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Detect rejection pattern
        if bar.delta > 0:  # Buying pressure
            # Expect upper wick (rejection of higher prices)
            upper_wick = bar.high - max(bar.open, bar.close)
            lower_wick = min(bar.open, bar.close) - bar.low

            # Buying absorption: upper wick > lower wick, doesn't extend above prior
            if upper_wick > lower_wick * 0.5 and bar.high <= prev_bar.high + 1.0:
                return (level_at_bar, Side.SELL, bar.low)

        elif bar.delta < 0:  # Selling pressure
            # Expect lower wick (rejection of lower prices)
            lower_wick = min(bar.open, bar.close) - bar.low
            upper_wick = bar.high - max(bar.open, bar.close)

            # Selling absorption: lower wick > upper wick, doesn't extend below prior
            if lower_wick > upper_wick * 0.5 and bar.low >= prev_bar.low - 1.0:
                return (level_at_bar, Side.BUY, bar.high)

        return None

    def _check_initiation(self, bar) -> bool:
        """
        Check if bar shows initiation: efficient price movement and large body
        relative to recent range.
        """
        if len(self.slow_bar_history) < 5:
            return False

        if bar.volume <= 0:
            return False

        # Body size relative to recent bars
        body = abs(bar.close - bar.open)

        recent = list(self.slow_bar_history)[-10:]
        if not recent:
            return False

        avg_range = sum(b.high - b.low for b in recent) / len(recent)
        if avg_range <= 0:
            return False

        body_ratio = body / avg_range

        # Require body to be significant fraction of recent range
        return body_ratio >= self.min_body_ratio
