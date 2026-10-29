"""
Investigates whether an outsized intraday move continues (trend continuation).
Builds on the prior findings by:
1. Increasing the outsized move threshold to 2.25x the session average.
2. Requiring the close to be even nearer the extreme (body > 80% of range).
3. Increasing the profit target to 2.5x the bar's range to capture longer runs.
4. Requiring the signed order flow (delta) to be at least 10% of the bar's volume.
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, 
                 multiplier: float = 2.25, size: int = 1,
                 stop_mult: float = 1.0, target_mult: float = 2.5,
                 body_frac: float = 0.8, min_delta_frac: float = 0.1):
        self.interval = interval
        self.min_bars = min_bars
        self.multiplier = multiplier
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.body_frac = body_frac
        self.min_delta_frac = min_delta_frac
        
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
            
        if avg_rng > 0 and body > self.multiplier * avg_rng and body > self.body_frac * rng:
            stop_offset = rng * self.stop_mult
            target_offset = rng * self.target_mult
            
            delta_frac = abs(last.delta) / last.volume if last.volume > 0 else 0.0
            
            if delta_frac >= self.min_delta_frac:
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
