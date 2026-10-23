"""
Investigates whether an outsized intraday move continues (trend continuation).
Builds on Trial #11 (+0.1561) by introducing VWAP alignment and time/exhaustion filters, 
while adjusting the primary threshold to maintain sufficient session exposure (>70%).

Key improvements:
1. VWAP Alignment: Requires the outsized move to close on the correct side of the 
   session VWAP in addition to the session open. This ensures alignment with the 
   volume-weighted institutional bias.
2. Threshold Calibration: Adjusts the outsized bar multiplier from 2.0x to 1.75x. 
   This compensates for the stricter VWAP trend requirement, ensuring the strategy 
   remains exposed in enough sessions to meet the 70% statistical validity floor.
3. Exhaustion & Time Filters: Adds a max_multiplier (4.0x) to avoid climactic news 
   candles, and an entry cutoff (last 40 mins) to ensure continuation trades have 
   enough time to reach their 2:1 target before the end-of-day flatten.

Maintains core parameters:
- Local 2-hour volatility baseline (24 bars)
- Bar closes near its extreme (body > 75% of range)
- Volume > 1.0x recent average volume
- Bracket: 1.0x range stop, 2.0x range target
"""

from __future__ import annotations


class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, window: int = 24,
                 multiplier: float = 1.75, max_multiplier: float = 4.0,
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
        self.sum_price_vol = 0.0
        self.sum_vol = 0

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
            self.sum_price_vol = 0.0
            self.sum_vol = 0
            
        history = ctx.history(self.interval)
        if len(history) == 0:
            return
            
        last = history[-1]
        
        if self.session_open is None:
            self.session_open = last.open
            
        rng = last.high - last.low
        body = abs(last.close - last.open)
        vol = last.volume
        
        # Update Session VWAP
        typ_price = (last.high + last.low + last.close) / 3.0
        self.sum_price_vol += typ_price * vol
        self.sum_vol += vol
        vwap = self.sum_price_vol / self.sum_vol if self.sum_vol > 0 else last.close
        
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
                    
                    # Long Condition
                    if last.close > last.open and last.delta > 0:
                        if last.close > self.session_open and last.close > vwap:
                            ctx.buy(
                                size=self.size, 
                                stop_offset=stop_offset, 
                                target_offset=target_offset, 
                                tag="continue-up"
                            )
                    # Short Condition
                    elif last.close < last.open and last.delta < 0:
                        if last.close < self.session_open and last.close < vwap:
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
