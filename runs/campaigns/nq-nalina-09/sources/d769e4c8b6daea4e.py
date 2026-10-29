"""
Investigates whether an outsized intraday move continues (trend continuation).
Builds on Trial #2 (+0.1107) by refining the definition of "average range":
Instead of using the entire session's average (which is heavily skewed by the highly volatile open),
it uses a rolling window (last 1 hour / 12 bars) to define the local average range.
This allows the strategy to adapt to midday quiet periods, where a smaller absolute move 
is still highly significant relative to recent price action.

Maintains the successful parameters from Trial #2:
- Bar body > 2.0x local average range
- Bar closes near its extreme (body > 75% of range)
- Delta agrees with direction
- 2:1 reward-to-risk bracket based on the outsized bar's range
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, rolling_window: int = 12,
                 multiplier: float = 2.0, size: int = 1,
                 stop_mult: float = 1.0, target_mult: float = 2.0):
        self.interval = interval
        self.min_bars = min_bars
        self.rolling_window = rolling_window
        self.multiplier = multiplier
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
            
        history = ctx.history(self.interval)
        if len(history) < self.min_bars + 1:
            return
            
        last = history[-1]
        
        # We need at least min_bars of prior history IN THE CURRENT SESSION
        window = history.last(self.rolling_window + 1)
        prior = window[:-1]
        prior_session_bars = [b for b in prior if b.session == ctx.session]
        
        if len(prior_session_bars) < self.min_bars:
            return
            
        avg_rng = sum(b.high - b.low for b in prior_session_bars) / len(prior_session_bars)
        
        rng = last.high - last.low
        body = abs(last.close - last.open)
        
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
