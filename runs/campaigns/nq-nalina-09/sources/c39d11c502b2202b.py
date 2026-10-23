"""
Investigates whether an outsized intraday move continues (trend continuation).
Builds on Trial #11 (+0.1561) by adding structural and order-flow confirmation, 
as well as optimizing the bracket for better trade management.

Key improvements over Trial #11:
1. Local Breakout Filter: The signal bar must not only be outsized (2.0x avg range), 
   but its close must also break the highest high (or lowest low) of the previous 
   30 minutes (6 bars). This ensures we are trading a genuine structural breakout 
   and not just a large bar trapped inside a recent high-volatility chop zone.
2. Order Flow Conviction: Requires the net delta to be at least 5% of the bar's 
   total volume in the direction of the move, ensuring aggressive institutional 
   participation rather than just a liquidity vacuum.
3. Expanded Bracket (Breathing Room): Increases the stop multiplier from 1.0x to 1.1x 
   the signal bar's range, and the target from 2.0x to 2.2x. This allows the trade 
   to survive minor inside-bar pullbacks that often follow momentum spikes, while 
   maintaining the 1:2 risk/reward ratio.
4. Late Entry Cutoff: Adds a 25-minute entry cutoff (10 minutes before the mandatory 
   flatten) to prevent entering trades that have no time to reach the target.

Maintains core parameters from #11:
- Local 2-hour volatility baseline (24 bars)
- Bar body > 2.0x recent average range
- Volume > 1.0x recent average volume
- Daily bias alignment (close vs session open)
"""

from __future__ import annotations


class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, window: int = 24,
                 multiplier: float = 2.0, vol_multiplier: float = 1.0, size: int = 1,
                 stop_mult: float = 1.1, target_mult: float = 2.2,
                 flatten_minutes: int = 15, entry_cutoff_minutes: int = 25,
                 breakout_bars: int = 6, delta_ratio: float = 0.05):
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
        self.breakout_bars = breakout_bars
        self.delta_ratio = delta_ratio
        
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
        
        # Accumulate baseline data
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
            
        # Ensure we have enough history to check the local breakout
        if len(history) <= self.breakout_bars:
            return
            
        # Determine local structural extremes
        recent_b_bars = history.last(self.breakout_bars + 1)[:-1]
        local_high = max(b.high for b in recent_b_bars)
        local_low = min(b.low for b in recent_b_bars)
        
        # Continuation signal logic
        if avg_rng > 0 and body > self.multiplier * avg_rng and body > 0.75 * rng:
            if vol > self.vol_multiplier * avg_vol:
                stop_offset = rng * self.stop_mult
                target_offset = rng * self.target_mult
                
                # Long Condition
                if last.close > last.open and last.delta > self.delta_ratio * vol:
                    if last.close > self.session_open and last.close > local_high:
                        ctx.buy(
                            size=self.size, 
                            stop_offset=stop_offset, 
                            target_offset=target_offset, 
                            tag="continue-up"
                        )
                # Short Condition
                elif last.close < last.open and last.delta < -self.delta_ratio * vol:
                    if last.close < self.session_open and last.close < local_low:
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
