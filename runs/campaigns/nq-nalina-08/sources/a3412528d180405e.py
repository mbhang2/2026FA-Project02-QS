"""Investigates whether outsized intra-session moves give it back (mean reversion).

Tests the Give-Back hypothesis: when a 5m bar's range is exceptionally large 
relative to the recent rolling average range, and closes near its extreme, we 
assume the move reflects a temporary liquidity imbalance. Price should drift 
back toward where it came from.

We enter in the opposite direction of the outsized move to capture the reversion, 
using a bracket order scaled to the size of the outsized bar. A minimum absolute 
range is required to avoid trading noise during quiet hours.
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", lookback: int = 10, 
                 range_mult: float = 2.5, min_range: float = 10.0,
                 stop_mult: float = 1.0, target_mult: float = 1.0, 
                 size: int = 1):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.min_range = min_range
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval, emit_empty=True)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
            
        # Flatten in the last 15 minutes of the session
        time_left = ctx.session_end - ctx.ts
        if time_left <= 15 * 60 * 1_000_000_000:
            if ctx.working_order_ids:
                ctx.cancel_all()
            if ctx.position:
                ctx.close()
            return
            
        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        window = history.last(self.lookback + 1)
        last = window[-1]
        prior = window[:-1]
        
        if last.tick_count == 0:
            return

        avg_range = sum((b.high - b.low) for b in prior) / self.lookback
        rng = last.high - last.low
        
        if avg_range > 0 and rng > self.range_mult * avg_range and rng >= self.min_range:
            
            is_bullish_exhaustion = (
                last.close > last.open and 
                (last.high - last.close) <= 0.25 * rng
            )
            
            is_bearish_exhaustion = (
                last.close < last.open and 
                (last.close - last.low) <= 0.25 * rng
            )
            
            if is_bullish_exhaustion:
                ctx.sell(
                    size=self.size,
                    stop_offset=rng * self.stop_mult,
                    target_offset=rng * self.target_mult,
                    tag="fade-up"
                )
            elif is_bearish_exhaustion:
                ctx.buy(
                    size=self.size,
                    stop_offset=rng * self.stop_mult,
                    target_offset=rng * self.target_mult,
                    tag="fade-down"
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
