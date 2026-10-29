"""
Investigates whether an outsized intraday move continues (trend continuation).
Builds on Trial #11 (+0.1561) by adding two institutional conviction filters:
1. Session VWAP Alignment: In addition to the daily open, the outsized move must now 
   also close on the correct side of the Session VWAP. This ensures the trade aligns 
   with the volume-weighted institutional bias for the day.
2. Order Flow Conviction (Delta %): Requires the outsized bar to have a net delta 
   of at least 10% of its volume. A large price move with neutral delta is often a 
   liquidity vacuum (prone to give-back). A large move with strong directional delta 
   shows aggressive participation (more likely to continue).

Maintains the core Trial #11 parameters:
- Local 2-hour volatility baseline (24 bars)
- Bar body > 2.0x recent average range
- Bar closes near its extreme (body > 75% of range)
- Volume > 1.0x recent average volume
- Bracket: 1.0x range stop, 2.0x range target
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, window: int = 24,
                 multiplier: float = 2.0, vol_multiplier: float = 1.0, size: int = 1,
                 stop_mult: float = 1.0, target_mult: float = 2.0,
                 flatten_minutes: int = 15, delta_pct: float = 0.10):
        self.interval = interval
        self.min_bars = min_bars
        self.window = window
        self.multiplier = multiplier
        self.vol_multiplier = vol_multiplier
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.flatten_minutes = flatten_minutes
        self.delta_pct = delta_pct
        
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
            
        if avg_rng > 0 and body > self.multiplier * avg_rng and body > 0.75 * rng:
            if vol > self.vol_multiplier * avg_vol:
                stop_offset = rng * self.stop_mult
                target_offset = rng * self.target_mult
                
                # Long Condition
                if last.close > last.open and last.delta > self.delta_pct * vol:
                    if last.close > self.session_open and last.close > vwap:
                        ctx.buy(
                            size=self.size, 
                            stop_offset=stop_offset, 
                            target_offset=target_offset, 
                            tag="continue-up"
                        )
                # Short Condition
                elif last.close < last.open and last.delta < -self.delta_pct * vol:
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
