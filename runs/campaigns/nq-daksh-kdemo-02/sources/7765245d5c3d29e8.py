"""
Investigates whether an outsized intraday move tends to continue.
Measures the directional move (close - open) of each 5m bar against the average 
true range (high - low) of the preceding 5m bars in the same session.
If the current bar's directional move exceeds a multiple of that average,
it enters in the direction of the momentum, betting on continuation.
"""

class Strategy:
    def __init__(self, interval="5m", min_bars=6, move_multiple=1.75, 
                 stop_offset=20.0, target_offset=40.0, size=1):
        self.interval = interval
        self.min_bars = min_bars
        self.move_multiple = move_multiple
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        
        self.session_id = None
        self.session_ranges = []

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if self.interval not in ctx.bars:
            return
            
        history = ctx.history(self.interval)
        if not history:
            return
            
        last = history[-1]
        
        # Track session ranges
        if self.session_id != last.session:
            self.session_id = last.session
            self.session_ranges = []
            
        self.session_ranges.append(last.high - last.low)

        if not ctx.actionable:
            return

        # Flatten near session end (5 minutes before close)
        time_left = ctx.session_end - ctx.ts
        if time_left < 5 * 60 * 1_000_000_000:
            if ctx.working_order_ids:
                ctx.cancel_all()
            if ctx.position:
                ctx.close()
            return

        if ctx.position or ctx.working_order_ids:
            return
            
        if len(self.session_ranges) < self.min_bars + 1:
            return
            
        prev_ranges = self.session_ranges[:-1]
        avg_range = sum(prev_ranges) / len(prev_ranges)
        
        if avg_range == 0:
            return
            
        move = last.close - last.open
        
        if abs(move) > self.move_multiple * avg_range:
            if move > 0:
                ctx.buy(size=self.size, stop_offset=self.stop_offset, target_offset=self.target_offset, tag="continue_up")
            else:
                ctx.sell(size=self.size, stop_offset=self.stop_offset, target_offset=self.target_offset, tag="continue_down")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
