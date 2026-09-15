"""
Adaptive momentum strategy with a strength filter.
Trades in the direction of the last bar, but only if the bar closed near its extreme,
indicating strong directional momentum. This filters out weak signals and helps
overcome the unfavorable fill model and commissions.
Uses the Average True Range (ATR) of the recent window to size the brackets,
allowing the strategy to adapt to the synthetic dataset's volatility.
"""

from __future__ import annotations


class Strategy:
    def __init__(self, interval: str = "5m", window: int = 14,
                 stop_mult: float = 1.0, target_mult: float = 2.0, 
                 strength_threshold: float = 0.3, size: int = 1):
        self.interval = interval
        self.window = window
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.strength_threshold = strength_threshold
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < self.window:
            return
            
        window_bars = history.last(self.window)
        
        # Calculate average bar range over the window
        avg_range = sum(b.high - b.low for b in window_bars) / self.window
        
        # Fallback for extremely low volatility to avoid tiny brackets
        if avg_range < 0.25:
            avg_range = 0.5
            
        last = window_bars[-1]
        bar_range = last.high - last.low
        
        if bar_range == 0:
            return
            
        stop_offset = avg_range * self.stop_mult
        target_offset = avg_range * self.target_mult
        
        # Buy if it's an up bar and closed in the top X% of its range
        if last.close > last.open and (last.high - last.close) <= self.strength_threshold * bar_range:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="strong-mom-long"
            )
        # Sell if it's a down bar and closed in the bottom X% of its range
        elif last.close < last.open and (last.close - last.low) <= self.strength_threshold * bar_range:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="strong-mom-short"
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
