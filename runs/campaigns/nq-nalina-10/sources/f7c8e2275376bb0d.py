"""
Investigates the "give-back" hypothesis for outsized intraday moves.

Measures the range of each 5-minute bar and keeps a running average for the
current session. If a bar's range is more than 2.5x the session's average so
far, and it closes strongly in one direction (body > 70% of range), we fade it
(trade in the opposite direction) on the assumption that it was a temporary
liquidity gap or climax that will revert.

Bracket is set to 1x the outsized bar's range for both target and stop.
New entries are avoided in the last 30 minutes of the session to allow time
for the reversion to play out intraday.
"""

from __future__ import annotations

class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        min_bars: int = 12,
        multiplier: float = 2.5,
        body_ratio: float = 0.7,
        size: int = 1,
    ):
        self.interval = interval
        self.min_bars = min_bars
        self.multiplier = multiplier
        self.body_ratio = body_ratio
        self.size = size
        
        self._session = None
        self._ranges: list[float] = []

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
            
        rng = last.high - last.low
        body = last.close - last.open
        
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            self._ranges.append(rng)
            return
            
        time_left = ctx.session_end - ctx.ts
        if time_left < 30 * 60 * 1_000_000_000:
            self._ranges.append(rng)
            return
            
        if len(self._ranges) >= self.min_bars:
            avg_range = sum(self._ranges) / len(self._ranges)
            if avg_range > 0 and rng > self.multiplier * avg_range:
                if abs(body) > self.body_ratio * rng:
                    stop_dist = rng
                    target_dist = rng
                    
                    if body > 0:
                        ctx.sell(
                            size=self.size,
                            stop_offset=stop_dist,
                            target_offset=target_dist,
                            tag="give-back-short"
                        )
                    else:
                        ctx.buy(
                            size=self.size,
                            stop_offset=stop_dist,
                            target_offset=target_dist,
                            tag="give-back-long"
                        )
                        
        self._ranges.append(rng)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
