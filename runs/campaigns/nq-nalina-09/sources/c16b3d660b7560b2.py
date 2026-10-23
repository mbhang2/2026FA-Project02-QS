"""
Investigates whether an outsized intraday move continues (trend continuation).
Measures the average true range of the 5-minute bars in the current session. 
If a bar's directional move (body) exceeds 1.5x the session's average bar range 
so far, and it closes near its extreme (body > 70% of its range), we assume 
information has arrived and trade in the direction of the move.
The position is protected by a bracket and any open position is flattened 
15 minutes before the session ends.
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, 
                 multiplier: float = 1.5, size: int = 1,
                 stop_mult: float = 1.0, target_mult: float = 1.5):
        self.interval = interval
        self.min_bars = min_bars
        self.multiplier = multiplier
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        
        self.session = None
        self.bar_ranges: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
            
        if ctx.session != self.session:
            self.session = ctx.session
            self.bar_ranges.clear()
            
        history = ctx.history(self.interval)
        if len(history) == 0:
            return
            
        last = history[-1]
        rng = last.high - last.low
        body = abs(last.close - last.open)
        
        if len(self.bar_ranges) < self.min_bars:
            self.bar_ranges.append(rng)
            return
            
        avg_rng = sum(self.bar_ranges) / len(self.bar_ranges)
        self.bar_ranges.append(rng)
        
        if not ctx.actionable:
            return
            
        # Flatten near session end (15 minutes)
        time_left = ctx.session_end - ctx.ts
        if time_left <= 15 * 60 * 1_000_000_000:
            if ctx.working_order_ids:
                ctx.cancel_all()
            if ctx.position:
                ctx.close()
            return
            
        if ctx.position or ctx.working_order_ids:
            return
            
        if avg_rng > 0 and body > self.multiplier * avg_rng and body > 0.7 * rng:
            stop_offset = rng * self.stop_mult
            target_offset = rng * self.target_mult
            
            if last.close > last.open:
                ctx.buy(
                    size=self.size, 
                    stop_offset=stop_offset, 
                    target_offset=target_offset, 
                    tag="continue-up"
                )
            elif last.close < last.open:
                ctx.sell(
                    size=self.size, 
                    stop_offset=stop_offset, 
                    target_offset=target_offset, 
                    tag="continue-down"
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
