"""
Investigates the "continuation" hypothesis for outsized intraday moves, filtered
by volume to avoid air pockets / low-liquidity spikes.

Measures the range and volume of each 5-minute bar, keeping a running average
for the current session. If a bar's range is more than 2.5x the session's average
so far, its volume is at least 1.25x the session's average, and it closes strongly
in one direction (body > 70% of range), we trade in the same direction. The volume
filter ensures the move reflects genuine participation and information arrival.

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
        vol_multiplier: float = 1.25,
        body_ratio: float = 0.7,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.min_bars = min_bars
        self.multiplier = multiplier
        self.vol_multiplier = vol_multiplier
        self.body_ratio = body_ratio
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        
        self._session = None
        self._ranges: list[float] = []
        self._volumes: list[float] = []
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
            self._volumes = []
            self._eod_closed = False
            
        rng = last.high - last.low
        body = last.close - last.open
        vol = last.volume
        
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
            self._volumes.append(vol)
            return
            
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            self._ranges.append(rng)
            self._volumes.append(vol)
            return
            
        # Avoid new entries in the last 30 minutes
        if time_left < 30 * 60 * 1_000_000_000:
            self._ranges.append(rng)
            self._volumes.append(vol)
            return
            
        if len(self._ranges) >= self.min_bars:
            avg_range = sum(self._ranges) / len(self._ranges)
            avg_vol = sum(self._volumes) / len(self._volumes)
            
            if avg_range > 0 and rng > self.multiplier * avg_range:
                if avg_vol > 0 and vol > self.vol_multiplier * avg_vol:
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
        self._volumes.append(vol)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
