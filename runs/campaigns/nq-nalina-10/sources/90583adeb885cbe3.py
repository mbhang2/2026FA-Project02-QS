"""
Investigates the "continuation" hypothesis for outsized intraday moves.

Measures the range of each 5-minute bar and keeps a running average for the
current session. If a bar's range is more than 2.5x the session's average so
far, and it closes strongly in one direction (body > 75% of range), we trade in
the same direction on the assumption that the move reflects new information and
flow has further to run.

Bracket is set to 0.8x the outsized bar's range for the stop (placing it near
the open of the outsized bar) and 1.6x for the target.
New entries are avoided in the last 30 minutes of the session, and any open
position is flattened 5 minutes before the session ends.
"""

from __future__ import annotations

class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        min_bars: int = 12,
        multiplier: float = 2.5,
        body_ratio: float = 0.75,
        stop_mult: float = 0.8,
        target_mult: float = 1.6,
        size: int = 1,
    ):
        self.interval = interval
        self.min_bars = min_bars
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
            
        if len(self._ranges) >= self.min_bars:
            avg_range = sum(self._ranges) / len(self._ranges)
            
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
