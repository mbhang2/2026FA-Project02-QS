"""
Investigates the "continuation" hypothesis for outsized intraday moves, focusing
on local volatility breakouts.

Instead of the session's expanding average range, this measures the range of each
5-minute bar against a rolling window (the last hour). If a bar's range is
more than 2.5x the recent average, closes strongly in one direction (body > 70%
of range), AND breaks out beyond the highest high (or lowest low) of that same
rolling window, we trade in the breakout direction.

This isolates genuine new intraday momentum from large bounces within a range.
Bracket is set to 1.0x the outsized bar's range for the stop and 2.0x for the target.
New entries are avoided in the last 30 minutes of the session, and any open
position is flattened 5 minutes before the session ends.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 12,
        multiplier: float = 2.5,
        body_ratio: float = 0.7,
        stop_mult: float = 1.0,
        target_mult: float = 2.0,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.multiplier = multiplier
        self.body_ratio = body_ratio
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        
        self._session = None
        self._ranges: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
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
            self._highs = []
            self._lows = []
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
            self._highs.append(last.high)
            self._lows.append(last.low)
            return
            
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            self._ranges.append(rng)
            self._highs.append(last.high)
            self._lows.append(last.low)
            return
            
        # Avoid new entries in the last 30 minutes
        if time_left < 30 * 60 * 1_000_000_000:
            self._ranges.append(rng)
            self._highs.append(last.high)
            self._lows.append(last.low)
            return
            
        if len(self._ranges) >= self.lookback:
            recent_ranges = self._ranges[-self.lookback:]
            avg_range = sum(recent_ranges) / self.lookback
            
            recent_highs = self._highs[-self.lookback:]
            recent_lows = self._lows[-self.lookback:]
            
            highest_high = max(recent_highs)
            lowest_low = min(recent_lows)
            
            if avg_range > 0 and rng > self.multiplier * avg_range:
                if abs(body) > self.body_ratio * rng:
                    stop_dist = rng * self.stop_mult
                    target_dist = rng * self.target_mult
                    
                    if body > 0 and last.close > highest_high:
                        ctx.buy(
                            size=self.size,
                            stop_offset=stop_dist,
                            target_offset=target_dist,
                            tag="continue-long"
                        )
                    elif body < 0 and last.close < lowest_low:
                        ctx.sell(
                            size=self.size,
                            stop_offset=stop_dist,
                            target_offset=target_dist,
                            tag="continue-short"
                        )
                        
        self._ranges.append(rng)
        self._highs.append(last.high)
        self._lows.append(last.low)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
