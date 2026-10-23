"""
Investigates whether an outsized intraday move continues (trend continuation).
Builds on Trial #2 (+0.1107) by adding two filters to increase the win rate:
1. Volume confirmation: The outsized bar's volume must be > 1.25x the session average.
2. Session trend alignment: The outsized move must be in the direction of the 
   session's overall net move (close vs session open).
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, 
                 multiplier: float = 2.0, size: int = 1,
                 stop_mult: float = 1.0, target_mult: float = 2.0,
                 vol_mult: float = 1.25):
        self.interval = interval
        self.min_bars = min_bars
        self.multiplier = multiplier
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.vol_mult = vol_mult
        
        self.session = None
        self.bar_ranges: list[float] = []
        self.bar_vols: list[float] = []
        self.session_open_price = 0.0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
            
        history = ctx.history(self.interval)
        if len(history) == 0:
            return
            
        last = history[-1]
        
        if ctx.session != self.session:
            self.session = ctx.session
            self.bar_ranges.clear()
            self.bar_vols.clear()
            self.session_open_price = last.open
            
        rng = last.high - last.low
        body = abs(last.close - last.open)
        vol = last.volume
        
        if len(self.bar_ranges) < self.min_bars:
            self.bar_ranges.append(rng)
            self.bar_vols.append(vol)
            return
            
        avg_rng = sum(self.bar_ranges) / len(self.bar_ranges)
        avg_vol = sum(self.bar_vols) / len(self.bar_vols)
        
        self.bar_ranges.append(rng)
        self.bar_vols.append(vol)
        
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
            
        if avg_rng > 0 and body > self.multiplier * avg_rng and body > 0.75 * rng and vol > self.vol_mult * avg_vol:
            stop_offset = rng * self.stop_mult
            target_offset = rng * self.target_mult
            
            if last.close > last.open and last.delta > 0 and last.close > self.session_open_price:
                ctx.buy(
                    size=self.size, 
                    stop_offset=stop_offset, 
                    target_offset=target_offset, 
                    tag="continue-up"
                )
            elif last.close < last.open and last.delta < 0 and last.close < self.session_open_price:
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
