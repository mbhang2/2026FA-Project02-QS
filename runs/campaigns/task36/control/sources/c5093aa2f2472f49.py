"""Moving Average Crossover with ATR-based brackets.

This strategy calculates a fast and slow simple moving average (SMA) to identify
trend direction and entries based on crossovers. Because the price scale and 
volatility of the instrument (especially a simulated random walk) are unknown, 
it uses the Average True Range (ATR) to dynamically size the stop loss and 
take profit brackets, ensuring the offsets are proportional to recent market 
volatility.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        fast_period: int = 10,
        slow_period: int = 30,
        atr_period: int = 14,
        size: int = 1,
        stop_mult: float = 1.0,
        target_mult: float = 2.0,
    ):
        self.interval = interval
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.atr_period = atr_period
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        lookback = max(self.slow_period, self.atr_period) + 1
        
        if len(history) < lookback:
            return

        window = history.last(lookback)
        
        # Calculate fast SMA for current and previous bar
        fast_curr = sum(b.close for b in window[-self.fast_period:]) / self.fast_period
        fast_prev = sum(b.close for b in window[-(self.fast_period + 1):-1]) / self.fast_period

        # Calculate slow SMA for current and previous bar
        slow_curr = sum(b.close for b in window[-self.slow_period:]) / self.slow_period
        slow_prev = sum(b.close for b in window[-(self.slow_period + 1):-1]) / self.slow_period

        # Calculate ATR over the specified period
        tr_sum = 0.0
        for i in range(-self.atr_period, 0):
            current = window[i]
            previous = window[i - 1]
            tr1 = current.high - current.low
            tr2 = abs(current.high - previous.close)
            tr3 = abs(current.low - previous.close)
            tr_sum += max(tr1, tr2, tr3)
        
        atr = tr_sum / self.atr_period
        if atr <= 0:
            return

        crossover_up = fast_prev <= slow_prev and fast_curr > slow_curr
        crossover_down = fast_prev >= slow_prev and fast_curr < slow_curr

        stop_offset = atr * self.stop_mult
        target_offset = atr * self.target_mult

        if crossover_up:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="ma-cross-long"
            )
        elif crossover_down:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="ma-cross-short"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
