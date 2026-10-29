"""
Investigates the "continuation" hypothesis for outsized intraday moves.

Measures the range of each 5-minute bar and keeps a rolling window of the last
12 bars to establish the local volatility baseline.
If a bar's range is more than 2.25x this local average, and it closes strongly
in one direction (body > 65% of range), we trade in the same direction on the
assumption that the move reflects new information and flow has further to run.

Bracket is set to 1.0x the outsized bar's range for the stop and 1.5x for the target.
New entries are avoided in the last 30 minutes of the session, and any open
position is flattened 5 minutes before the session ends to ensure we only
measure the intraday continuation.
"""

from __future__ import annotations

class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 12,
        multiplier: float = 2.25,
        body_ratio: float = 0.65,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.multiplier = multiplier
        self.body_ratio = body_ratio
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
            
        if len(self._ranges) >= self.lookback:
            recent_ranges = self._ranges[-self.lookback:]
            avg_range = sum(recent_ranges) / len(recent_ranges)
            
            if avg_range > 0 and rng > self.multiplier * avg_range:
                if abs(body) > self.body_ratio * rng:
                    stop_dist = rng * 1.0
                    target_dist = rng * 1.5
                    
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
