"""
Investigates whether an outsized intraday move continues (trend continuation).
Builds on Trial #7 (+0.1125) by increasing the quality requirements for the outsized bar.
1. Increases the required body-to-range ratio from 75% to 80%, ensuring the bar closes 
   even closer to its extreme.
2. Adds a delta fraction requirement: the net aggressive order flow (delta) must be at least 
   10% of the bar's total volume. This ensures the outsized move was driven by a genuine 
   imbalance in aggressive buying/selling, rather than just wide spreads on low liquidity.
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, 
                 multiplier: float = 2.0, vol_multiplier: float = 1.0, 
                 body_frac: float = 0.80, delta_frac: float = 0.10,
                 size: int = 1, stop_mult: float = 1.0, target_mult: float = 2.0,
                 flatten_minutes: int = 15):
        self.interval = interval
        self.min_bars = min_bars
        self.multiplier = multiplier
        self.vol_multiplier = vol_multiplier
        self.body_frac = body_frac
        self.delta_frac = delta_frac
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.flatten_minutes = flatten_minutes
        
        self.session = None
        self.bar_ranges: list[float] = []
        self.bar_volumes: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
            
        if ctx.session != self.session:
            self.session = ctx.session
            self.bar_ranges.clear()
            self.bar_volumes.clear()
            
        history = ctx.history(self.interval)
        if len(history) == 0:
            return
            
        last = history[-1]
        rng = last.high - last.low
        body = abs(last.close - last.open)
        vol = last.volume
        
        if len(self.bar_ranges) < self.min_bars:
            self.bar_ranges.append(rng)
            self.bar_volumes.append(vol)
            return
            
        avg_rng = sum(self.bar_ranges) / len(self.bar_ranges)
        avg_vol = sum(self.bar_volumes) / len(self.bar_volumes)
        
        self.bar_ranges.append(rng)
        self.bar_volumes.append(vol)
        
        if not ctx.actionable:
            return
            
        # Flatten near session end
        time_left = ctx.session_end - ctx.ts
        if time_left <= self.flatten_minutes * 60 * 1_000_000_000:
            if ctx.working_order_ids:
                ctx.cancel_all()
            if ctx.position:
                ctx.close()
            return
            
        if ctx.position or ctx.working_order_ids:
            return
            
        if avg_rng > 0 and body > self.multiplier * avg_rng and body > self.body_frac * rng:
            if vol > self.vol_multiplier * avg_vol and abs(last.delta) > self.delta_frac * vol:
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
