"""
Investigates whether an outsized intraday move continues (trend continuation).
Builds on Trial #11 (+0.1561) by refining the quality of the continuation signal 
while maintaining high session exposure. 

Key improvements over Trial #11:
1. Volume and Order Flow Confirmation: Increases the volume requirement from 1.0x 
   to 1.2x the recent average, and introduces a Delta Imbalance filter. The net 
   delta (buying vs selling pressure) must be at least 5% of the bar's total volume 
   in the direction of the move. This filters out large bars driven by two-way 
   chop and ensures institutional participation.
2. Threshold Calibration: Lowers the range multiplier slightly from 2.0x to 1.8x. 
   Since the volume and delta filters are stricter, lowering the range threshold 
   ensures we capture enough high-quality setups to remain safely above the 70% 
   session exposure floor (which was breached in Trial #15).
3. Late Session Entry Cutoff: Adds a 35-minute entry cutoff. Trades entered too 
   close to the end of the session do not have enough time to reach the 2.0x target 
   before the mandatory 15-minute end-of-day flatten, leading to sub-optimal exits.

Maintains core parameters from #11:
- Local 2-hour volatility baseline (24 bars)
- Daily bias alignment (close vs session open)
- Bar closes near its extreme (body > 75% of range)
- Bracket: 1.0x range stop (places stop just below/above the signal bar), 
  2.0x range target.
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, window: int = 24,
                 multiplier: float = 1.8, vol_multiplier: float = 1.2, size: int = 1,
                 stop_mult: float = 1.0, target_mult: float = 2.0,
                 flatten_minutes: int = 15, entry_cutoff_minutes: int = 35,
                 delta_ratio: float = 0.05, body_threshold: float = 0.75):
        self.interval = interval
        self.min_bars = min_bars
        self.window = window
        self.multiplier = multiplier
        self.vol_multiplier = vol_multiplier
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.flatten_minutes = flatten_minutes
        self.entry_cutoff_minutes = entry_cutoff_minutes
        self.delta_ratio = delta_ratio
        self.body_threshold = body_threshold
        
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
        
        # Accumulate baseline data during warmup and early session
        if len(self.bar_ranges) < self.min_bars:
            self.bar_ranges.append(rng)
            self.bar_volumes.append(vol)
            return
            
        # Calculate recent averages (excluding the current bar)
        recent_ranges = self.bar_ranges[-self.window:]
        recent_vols = self.bar_volumes[-self.window:]
        
        avg_rng = sum(recent_ranges) / len(recent_ranges)
        avg_vol = sum(recent_vols) / len(recent_vols)
        
        # Update baseline for the next bar
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
            
        # Continuation signal logic
        if avg_rng > 0 and body > self.multiplier * avg_rng and body > self.body_threshold * rng:
            if vol > self.vol_multiplier * avg_vol:
                stop_offset = rng * self.stop_mult
                target_offset = rng * self.target_mult
                
                # Long Condition
                if last.close > last.open and last.delta > self.delta_ratio * vol:
                    if last.close > self.session_open:
                        ctx.buy(
                            size=self.size, 
                            stop_offset=stop_offset, 
                            target_offset=target_offset, 
                            tag="continue-up"
                        )
                # Short Condition
                elif last.close < last.open and last.delta < -self.delta_ratio * vol:
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
