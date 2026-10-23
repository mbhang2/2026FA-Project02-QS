"""
Investigates whether an outsized intraday move continues (trend continuation).
Builds on the prior finding that continuation is profitable by refining the signal:
1. Requires the bar's body to be > 2.0x the session's average bar range (stronger move).
2. Requires the bar to close very near its extreme (body > 75% of range).
3. Requires the bar's signed order flow (delta) to agree with the move direction.
Targets a 2:1 reward-to-risk ratio based on the size of the outsized bar.
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, 
                 multiplier: float = 2.0, size: int = 1,
                 stop_mult: float = 1.0, target_mult: float = 2.0):
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
