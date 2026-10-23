"""
Investigates the "continuation" hypothesis for outsized intraday moves, enhanced
by aligning the trade with the overall session trend.

Measures the range of each 5-minute bar and keeps a running average for the
current session. If a bar's range is more than 2.5x the session's average so
far, and it closes strongly in one direction (body > 70% of range), we check
if the close is aligned with the session's opening price. If so, we trade in
that direction, assuming the outsized move is a continuation of the daily trend
and has further to run.

Bracket is set to 1.0x the outsized bar's range for the stop and 1.5x for the target.
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
        body_ratio: float = 0.7,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
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
        self._session_open = 0.0

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
            self._session_open = last.open
            
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
                    
                    # Align with session trend
                    if body > 0 and last.close > self._session_open:
                        ctx.buy(
                            size=self.size,
                            stop_offset=stop_dist,
                            target_offset=target_dist,
                            tag="continue-long"
                        )
                    elif body < 0 and last.close < self._session_open:
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
