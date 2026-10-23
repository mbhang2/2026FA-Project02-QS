"""
Investigates whether an outsized intraday move continues (trend continuation).
Builds on Trial #11 (+0.1561) by introducing three key improvements to survivability and conviction:
1. Stop Buffer: Increases stop_mult from 1.0 to 1.25. Placing a stop exactly at 1.0x the bar's 
   range often leaves it exactly at the low/high of the signal bar, making it highly vulnerable 
   to single-tick sweeps before the continuation occurs. A 1.25x multiplier places the stop safely 
   beyond the extreme of the outsized bar.
2. Proportional Target: Increases target_mult from 2.0 to 2.5 to maintain the 2:1 reward-to-risk 
   ratio against the wider stop.
3. Volume Conviction: Increases vol_multiplier from 1.0 to 1.2, requiring the outsized move to 
   have at least 20% more volume than the recent baseline, filtering out low-liquidity price spikes.
4. Absolute Range Filter: Adds a minimum range requirement (rng > 10.0 points) to ensure the 
   strategy doesn't trigger on micro-bars during the ultra-quiet Asian session where "2x average" 
   might only be a 5-point move that gets eaten by the spread.

Maintains:
- Local 2-hour volatility baseline (24 bars)
- Daily bias alignment (close vs session open)
- Bar closes near its extreme (body > 75% of range)
- Delta agrees with direction
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, window: int = 24,
                 multiplier: float = 2.0, vol_multiplier: float = 1.2, size: int = 1,
                 stop_mult: float = 1.25, target_mult: float = 2.5,
                 flatten_minutes: int = 15):
        self.interval = interval
        self.min_bars = min_bars
        self.window = window
        self.multiplier = multiplier
        self.vol_multiplier = vol_multiplier
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.flatten_minutes = flatten_minutes
        
        self.session = None
        self.session_open = None
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
            self.session_open = None
            
        history = ctx.history(self.interval)
        if len(history) == 0:
            return
            
        last = history[-1]
        
        if self.session_open is None:
            self.session_open = last.open
            
        rng = last.high - last.low
        body = abs(last.close - last.open)
        vol = last.volume
        
        if len(self.bar_ranges) < self.min_bars:
            self.bar_ranges.append(rng)
            self.bar_volumes.append(vol)
            return
            
        recent_ranges = self.bar_ranges[-self.window:]
        recent_vols = self.bar_volumes[-self.window:]
        
        avg_rng = sum(recent_ranges) / len(recent_ranges)
        avg_vol = sum(recent_vols) / len(recent_vols)
        
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
            
        # Require a meaningful absolute range (10 points) to avoid micro-moves in quiet hours
        if avg_rng > 0 and rng > 10.0 and body > self.multiplier * avg_rng and body > 0.75 * rng:
            if vol > self.vol_multiplier * avg_vol:
                stop_offset = rng * self.stop_mult
                target_offset = rng * self.target_mult
                
                if last.close > last.open and last.delta > 0:
                    if last.close > self.session_open:
                        ctx.buy(
                            size=self.size, 
                            stop_offset=stop_offset, 
                            target_offset=target_offset, 
                            tag="continue-up"
                        )
                elif last.close < last.open and last.delta < 0:
                    if last.close < self.session_open:
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
