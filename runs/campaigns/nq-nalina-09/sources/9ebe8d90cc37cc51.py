"""
Investigates whether an outsized intraday move continues (trend continuation).
Builds on Trial #11 (+0.1561) by integrating the price progression requirement from Trial #10.
1. Local Volatility Baseline: rolling window of 24 bars to identify outsized moves.
2. Daily Bias Alignment: The move must align with the session's overall trend (close above session open for longs).
3. Price Progression: The outsized bar must break the previous bar's extreme (close > prev.high for longs), 
   ensuring it's a true initiation/continuation and not just a large bar inside a previous range.
4. Late Entry Filter: Prevents entering new positions in the last 45 minutes of the session, 
   as they lack time to reach the 2:1 target before the end-of-day flatten.

Maintains:
- Bar body > 2.0x recent average range
- Bar closes near its extreme (body > 75% of range)
- Volume > 1.0x recent average volume
- Delta agrees with direction
- 2:1 reward-to-risk bracket based on the outsized bar's range
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, window: int = 24,
                 multiplier: float = 2.0, vol_multiplier: float = 1.0, size: int = 1,
                 stop_mult: float = 1.0, target_mult: float = 2.0,
                 flatten_minutes: int = 15, no_entry_minutes: int = 45):
        self.interval = interval
        self.min_bars = min_bars
        self.window = window
        self.multiplier = multiplier
        self.vol_multiplier = vol_multiplier
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.flatten_minutes = flatten_minutes
        self.no_entry_minutes = no_entry_minutes
        
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
            
        if len(history) < 2:
            return
            
        prev = history[-2]
            
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
            
        # Do not enter new positions too close to session end
        if time_left <= self.no_entry_minutes * 60 * 1_000_000_000:
            return
            
        if avg_rng > 0 and body > self.multiplier * avg_rng and body > 0.75 * rng:
            if vol > self.vol_multiplier * avg_vol:
                stop_offset = rng * self.stop_mult
                target_offset = rng * self.target_mult
                
                if last.close > last.open and last.delta > 0:
                    if last.close > self.session_open and last.close > prev.high:
                        ctx.buy(
                            size=self.size, 
                            stop_offset=stop_offset, 
                            target_offset=target_offset, 
                            tag="continue-up"
                        )
                elif last.close < last.open and last.delta < 0:
                    if last.close < self.session_open and last.close < prev.low:
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
