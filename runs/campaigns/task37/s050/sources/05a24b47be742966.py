"""
A mean-reversion strategy based on signed volume imbalance.

The reference momentum strategy buys/sells based on OHLC alone. This strategy
adds order flow: it computes signed volume delta (buy_volume - sell_volume) over
a rolling window and uses it as a filter or signal.

Key ideas:
1. Mean reversion: extreme imbalances often reverse. A highly positive delta
   (more buying) may precede a pullback; highly negative delta may precede a
   rally.
2. Accumulate delta across bars to capture session-level imbalance.
3. Trade when delta crosses a threshold in the opposite direction (contrarian).
4. Use the same bracket protection as the reference strategy.

The strategy:
- Subscribes to bars (not ticks, to stay within budget).
- Accumulates buy_volume and sell_volume across bars on self.
- Computes rolling delta and its z-score or percentile.
- Enters contrarian positions when delta is extreme.
- Resets accumulation at session boundaries.

This avoids the 5x cost of tick subscription while still using order flow
(available on bars via buy_volume and sell_volume fields).
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 20,
        delta_threshold: float = 0.6,
        size: int = 1,
        stop_offset: float = 0.75,
        target_offset: float = 0.75,
    ):
        """
        Args:
            interval: bar interval to subscribe to
            lookback: number of bars to accumulate delta over
            delta_threshold: percentile threshold (0-1) for extreme imbalance
            size: contract size
            stop_offset: protective stop distance
            target_offset: profit target distance
        """
        self.interval = interval
        self.lookback = lookback
        self.delta_threshold = delta_threshold
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        
        # Accumulate deltas across bars
        self.deltas: deque[float] = deque(maxlen=lookback)
        self._last_session: int | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset accumulation on session boundary
        if ctx.session != self._last_session:
            self._last_session = ctx.session
            self.deltas.clear()
        
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        last = history[-1]
        
        # Accumulate delta from the last bar
        delta = float(last.buy_volume - last.sell_volume)
        self.deltas.append(delta)
        
        # Need enough history to compute threshold
        if len(self.deltas) < self.lookback:
            return
        
        # Compute mean and std of accumulated deltas
        deltas_list = list(self.deltas)
        mean_delta = sum(deltas_list) / len(deltas_list)
        variance = sum((d - mean_delta) ** 2 for d in deltas_list) / len(deltas_list)
        std_delta = variance ** 0.5
        
        if std_delta <= 0:
            return
        
        # Normalize current delta
        current_delta = deltas_list[-1]
        z_score = (current_delta - mean_delta) / std_delta
        
        # Mean reversion: trade opposite to extreme imbalance
        # If delta is extremely positive (z > threshold), sell (expect pullback)
        # If delta is extremely negative (z < -threshold), buy (expect rally)
        threshold = self.delta_threshold
        
        if z_score > threshold:
            # Extreme buying; expect mean reversion down
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="mr-short",
            )
        elif z_score < -threshold:
            # Extreme selling; expect mean reversion up
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="mr-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
