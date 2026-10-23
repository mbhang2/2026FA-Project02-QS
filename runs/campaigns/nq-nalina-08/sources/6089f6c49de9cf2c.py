"""Investigates whether outsized intra-session moves continue or give it back.

Tests the Continuation hypothesis using price and order flow: when a 5m bar's range 
is exceptionally large relative to the session's average bar range so far (adapting 
to the day's volatility), closes near its extreme, and is confirmed by directional 
delta (order flow), we assume the flow has further to run.

We enter in the direction of the outsized move and use a bracket order to capture 
the continuation. The bracket legs are scaled dynamically to the size of the breakout bar.
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 12, 
                 range_mult: float = 2.5, stop_mult: float = 1.0, 
                 target_mult: float = 2.0, size: int = 1):
        self.interval = interval
        self.min_bars = min_bars
        self.range_mult = range_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        
        self.session_idx: int | None = None
        self.session_ranges: list[float] = []

    def setup(self, ctx) -> None:
        # emit_empty=True ensures we get a callback to flatten at the end of the session
        # even if no trades occur in the final 5 minutes.
        ctx.subscribe(self.interval, emit_empty=True)

    def on_bars(self, ctx) -> None:
        # 1. Session boundary tracking
        if ctx.session != self.session_idx:
            self.session_idx = ctx.session
            self.session_ranges = []
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        
        # 2. Flatten and cancel working orders in the last 5 minutes of the session
        time_left = ctx.session_end - ctx.ts
        if time_left <= 5 * 60 * 1_000_000_000:
            if ctx.actionable:
                if ctx.working_order_ids:
                    ctx.cancel_all()
                if ctx.position:
                    ctx.close()
            return

        # 3. Skip empty bars for signal generation and baseline calculation
        if last.tick_count == 0:
            return

        rng = last.high - last.low
        
        # 4. Signal logic
        if len(self.session_ranges) >= self.min_bars and rng > 0:
            avg_range = sum(self.session_ranges) / len(self.session_ranges)
            
            if avg_range > 0 and rng > self.range_mult * avg_range:
                if ctx.actionable and not ctx.position and not ctx.working_order_ids:
                    # Continuation: enter in the direction of the extreme close, confirmed by delta
                    is_bullish = (last.close > last.open) and (last.high - last.close <= 0.25 * rng) and (last.delta > 0)
                    is_bearish = (last.close < last.open) and (last.close - last.low <= 0.25 * rng) and (last.delta < 0)
                    
                    if is_bullish:
                        ctx.buy(
                            size=self.size,
                            stop_offset=rng * self.stop_mult,
                            target_offset=rng * self.target_mult,
                            tag="continue-long"
                        )
                    elif is_bearish:
                        ctx.sell(
                            size=self.size,
                            stop_offset=rng * self.stop_mult,
                            target_offset=rng * self.target_mult,
                            tag="continue-short"
                        )
                        
        # 5. Update baseline with the current bar's range (done after signal check so 
        # the outsized bar itself doesn't skew the average we compare it against)
        self.session_ranges.append(rng)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
