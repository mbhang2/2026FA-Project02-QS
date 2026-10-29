"""
Investigates the "continuation" hypothesis for outsized intraday moves, using a 
rolling volatility window to define "outsized".

Builds on the prior expanding session average approach, but replaces the whole-session
average with a rolling 1-hour (12 bars) average. This allows the strategy to detect
moves that are outsized relative to the *recent* intraday context (e.g., a sudden
large move after a quiet period), rather than being anchored by the typically
high volatility of the morning open. 

If a 5-minute bar's range is more than 3.0x the average of the last 12 bars, and it
closes strongly in one direction (body > 75% of range), we trade in that direction.
Bracket is set to 1.0x the outsized bar's range for the stop and 1.5x for the target.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        window: int = 12,
        multiplier: float = 3.0,
        body_ratio: float = 0.75,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.window = window
        self.multiplier = multiplier
        self.body_ratio = body_ratio
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        
        self._session = None
        self._ranges: list[float] = []
        self._eod_closed = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        
        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges = []
            self._eod_closed = False
            
        rng = last.high - last.low
        body = last.close - last.open
        
        time_left = ctx.session_end - ctx.ts
        
        # Flatten any open position just before session close
        if time_left <= 5 * 60 * 1_000_000_000:
            if not self._eod_closed:
                if ctx.working_order_ids:
                    ctx.cancel_all()
                if ctx.position:
                    ctx.close()
                self._eod_closed = True
            self._ranges.append(rng)
            return
            
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            self._ranges.append(rng)
            return
            
        # Avoid new entries in the last 30 minutes
        if time_left < 30 * 60 * 1_000_000_000:
            self._ranges.append(rng)
            return
            
        if len(self._ranges) >= self.window:
            recent_ranges = self._ranges[-self.window:]
            avg_range = sum(recent_ranges) / self.window
            
            if avg_range > 0 and rng > self.multiplier * avg_range:
                if abs(body) > self.body_ratio * rng:
                    stop_dist = rng * self.stop_mult
                    target_dist = rng * self.target_mult
                    
                    if body > 0:
                        ctx.buy(
                            size=self.size,
                            stop_offset=stop_dist,
                            target_offset=target_dist,
                            tag="continue-long"
                        )
                    else:
                        ctx.sell(
                            size=self.size,
                            stop_offset=stop_dist,
                            target_offset=target_dist,
                            tag="continue-short"
                        )
                        
        self._ranges.append(rng)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
