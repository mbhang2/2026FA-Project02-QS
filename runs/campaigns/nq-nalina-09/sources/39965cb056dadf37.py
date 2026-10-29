"""
Investigates whether an outsized intraday move gives back its gains (mean reversion).
Measures the average true range of the 5-minute bars in the current session. 
If a bar's range exceeds 2.5x the session's average bar range so far (and we have 
at least 30 minutes of baseline), we fade the move: selling a large up-bar or 
buying a large down-bar. The bracket targets a 1:1 reward-to-risk based on the 
size of the outsized bar.
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, 
                 multiplier: float = 2.5, size: int = 1,
                 stop_mult: float = 1.0, target_mult: float = 1.0):
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
        
        if len(self.bar_ranges) < self.min_bars:
            self.bar_ranges.append(rng)
            return
            
        avg_rng = sum(self.bar_ranges) / len(self.bar_ranges)
        is_large = rng > self.multiplier * avg_rng and rng > 0
        
        # Append current range for future calculations
        self.bar_ranges.append(rng)
        
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        if is_large:
            stop_offset = rng * self.stop_mult
            target_offset = rng * self.target_mult
            
            if last.close > last.open:
                ctx.sell(
                    size=self.size, 
                    stop_offset=stop_offset, 
                    target_offset=target_offset, 
                    tag="fade-up"
                )
            elif last.close < last.open:
                ctx.buy(
                    size=self.size, 
                    stop_offset=stop_offset, 
                    target_offset=target_offset, 
                    tag="fade-down"
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
