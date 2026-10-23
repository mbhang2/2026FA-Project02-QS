"""
Investigates whether an outsized intraday move continues (trend continuation).
Builds on Trial #11 (+0.1561), which is the best performer so far.
Adds two logical filters to #11's core logic:
1. Late Session Entry Cutoff: Adds a 40-minute entry cutoff. Trades entered too 
   close to the end of the session do not have enough time to reach the 2.0x target 
   before the mandatory 15-minute end-of-day flatten, leading to sub-optimal exits 
   that just pay the spread.
2. Climactic Move Filter: Adds a max_multiplier (4.0x) to avoid entering on 
   extreme news-driven candles that are prone to immediate mean reversion.

Maintains the successful parameters from Trial #11:
- Local 2-hour volatility baseline (24 bars)
- Bar body > 2.0x recent average range
- Bar closes near its extreme (body > 75% of range)
- Volume > 1.0x recent average volume
- Delta agrees with direction
- Daily bias alignment (close vs session open)
- 2:1 reward-to-risk bracket based on the outsized bar's range
"""

from __future__ import annotations


class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, window: int = 24,
                 multiplier: float = 2.0, max_multiplier: float = 4.0,
                 vol_multiplier: float = 1.0, size: int = 1,
                 stop_mult: float = 1.0, target_mult: float = 2.0,
                 flatten_minutes: int = 15, entry_cutoff_minutes: int = 40):
        self.interval = interval
        self.min_bars = min_bars
        self.window = window
        self.multiplier = multiplier
        self.max_multiplier = max_multiplier
        self.vol_multiplier = vol_multiplier
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.flatten_minutes = flatten_minutes
        self.entry_cutoff_minutes = entry_cutoff_minutes
        
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
            
        time_left = ctx.session_end - ctx.ts
        
        # Flatten near session end
        if time_left <= self.flatten_minutes * 60 * 1_000_000_000:
            if ctx.working_order_ids:
                ctx.cancel_all()
            if ctx.position:
                ctx.close()
            return
            
        if ctx.position or ctx.working_order_ids:
            return
            
        # Prevent new entries if there isn't enough time for the trade to play out
        if time_left <= self.entry_cutoff_minutes * 60 * 1_000_000_000:
            return
            
        if avg_rng > 0 and self.multiplier * avg_rng < body < self.max_multiplier * avg_rng:
            if body > 0.75 * rng:
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
