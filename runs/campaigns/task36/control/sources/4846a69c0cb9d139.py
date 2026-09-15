"""
Reasoning:
The prompt indicates that the baseline strategy ("buy the next interval if the last one closed up, sell if it closed down") is "the dumbest strategy that can express the planted signal". This implies the simulated dataset contains a planted 1-bar momentum (positive autocorrelation) signal.

The baseline uses a symmetric bracket of 0.75 for both stop and target. To improve the score, we can:
1. Increase the target offset to capture more of the momentum trend (asymmetric risk/reward, e.g., target=1.5, stop=0.75) assuming the momentum persists for more than a minimal amount.
2. Maintain the core logic of following the 1-bar momentum to ensure we stay aligned with the planted signal.
3. Keep the strategy robust and simple to avoid execution errors or action cap hits.
"""

class Strategy:
    def __init__(self, interval="5m", stop_offset=0.75, target_offset=1.5, size=1):
        self.interval = interval
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
            
        last = history[-1]
        
        if last.close > last.open:
            ctx.buy(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="mom-long"
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size, 
                stop_offset=self.stop_offset, 
                target_offset=self.target_offset, 
                tag="mom-short"
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
