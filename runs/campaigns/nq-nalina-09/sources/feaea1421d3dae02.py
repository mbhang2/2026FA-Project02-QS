"""
Investigates whether an outsized intraday move continues (trend continuation).
Builds on Trial #2 (+0.1107) by refining the trade management and entry window:
1. Lowers the profit target from 2.0x to 1.5x the signal bar's range. An outsized bar 
   is already >2.0x the average range; expecting another 4x average range (2.0 target) 
   can limit the win rate. A 1.5x target captures the immediate follow-through more reliably.
2. Restricts new entries to avoid the last 60 minutes of the session, ensuring there 
   is enough time for the continuation to play out before the end-of-day flatten.
Maintains the core signal: body > 2.0x session average range, closing near extreme, with delta support.
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, 
                 multiplier: float = 2.0, size: int = 1,
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
            
        # No new entries in the last 60 minutes
        if time_left <= 60 * 60 * 1_000_000_000:
            return
            
        if avg_rng > 0 and body > self.multiplier * avg_rng and body > 0.75 * rng:
            stop_offset = rng * self.stop_mult
            target_offset = rng * self.target_mult
            
            if last.close > last.open and last.delta > 0:
                ctx.buy(
                    size=self.size, 
                    stop_offset=stop_offset, 
                    target_offset=target_offset, 
                    tag="continue-up"
                )
            elif last.close < last.open and last.delta < 0:
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
