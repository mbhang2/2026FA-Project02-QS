"""Investigates whether outsized intra-session moves continue.

Tests the Continuation hypothesis: when a 5m bar's body is exceptionally large 
relative to the session's average bar body so far, and it closes near its extreme, 
we assume the flow has further to run and enter in the direction of the move.

The threshold for "large" is dynamic (3.0x the session's average body so far), 
ensuring it adapts to the day's volatility rather than using a fixed point value.
"""

from __future__ import annotations

class Strategy:
    def __init__(self, interval: str = "5m", min_bars: int = 6, 
                 body_mult: float = 3.0, stop_mult: float = 1.0, 
                 target_mult: float = 1.5, size: int = 1):
        self.interval = interval
        self.min_bars = min_bars
        self.body_mult = body_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        
        self.session_idx: int | None = None
        self.session_bodies: list[float] = []

    def setup(self, ctx) -> None:
        # emit_empty=True ensures we get a callback to flatten at the end of the session
        # even if no trades occur in the final 5 minutes.
        ctx.subscribe(self.interval, emit_empty=True)

    def on_bars(self, ctx) -> None:
        if ctx.session != self.session_idx:
            self.session_idx = ctx.session
            self.session_bodies = []
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        
        # Flatten and cancel working orders in the last 5 minutes of the session
        time_left = ctx.session_end - ctx.ts
        if time_left <= 5 * 60 * 1_000_000_000:
            if ctx.working_order_ids:
                ctx.cancel_all()
            if ctx.position:
                ctx.close()
            return

        if not ctx.actionable:
            return

        is_empty = last.tick_count == 0
        if is_empty:
            return

        body = abs(last.close - last.open)
        rng = last.high - last.low
        
        if len(self.session_bodies) >= self.min_bars and rng > 0:
            avg_body = sum(self.session_bodies) / len(self.session_bodies)
            
            if avg_body > 0 and body > self.body_mult * avg_body:
                if not ctx.position and not ctx.working_order_ids:
                    # Continuation: enter if it closes in the top/bottom 30% of its range
                    if last.close > last.open and (last.high - last.close) <= 0.3 * rng:
                        ctx.buy(
                            size=self.size,
                            stop_offset=body * self.stop_mult,
                            target_offset=body * self.target_mult,
                            tag="continue-long"
                        )
                    elif last.close < last.open and (last.close - last.low) <= 0.3 * rng:
                        ctx.sell(
                            size=self.size,
                            stop_offset=body * self.stop_mult,
                            target_offset=body * self.target_mult,
                            tag="continue-short"
                        )
                        
        self.session_bodies.append(body)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
