"""SMA Crossover Strategy with ATR-based brackets.

A robust trend-following strategy that enters on a fast/slow SMA crossover
and uses ATR to set proportional stop-loss and take-profit brackets.
"""

class Strategy:
    def __init__(self, interval="5m", fast_window=10, slow_window=30, atr_window=14, atr_stop_mult=2.0, atr_target_mult=3.0, size=1):
        self.interval = interval
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.atr_window = atr_window
        self.atr_stop_mult = atr_stop_mult
        self.atr_target_mult = atr_target_mult
        self.size = size

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return
            
        lookback = max(self.slow_window + 1, self.atr_window + 1)
        history = ctx.history(self.interval)
        if len(history) < lookback:
            return
            
        bars = history.last(lookback)
        
        # Calculate SMAs for current and previous bar
        slow_bars = bars[-(self.slow_window + 1):]
        fast_bars = bars[-(self.fast_window + 1):]
        
        curr_slow_sma = sum(b.close for b in slow_bars[1:]) / self.slow_window
        prev_slow_sma = sum(b.close for b in slow_bars[:-1]) / self.slow_window
        
        curr_fast_sma = sum(b.close for b in fast_bars[1:]) / self.fast_window
        prev_fast_sma = sum(b.close for b in fast_bars[:-1]) / self.fast_window
        
        cross_up = curr_fast_sma > curr_slow_sma and prev_fast_sma <= prev_slow_sma
        cross_down = curr_fast_sma < curr_slow_sma and prev_fast_sma >= prev_slow_sma
        
        if not (cross_up or cross_down):
            return
            
        # Calculate ATR
        atr_bars = bars[-(self.atr_window + 1):]
        tr_sum = 0.0
        for i in range(1, self.atr_window + 1):
            high = atr_bars[i].high
            low = atr_bars[i].low
            prev_close = atr_bars[i-1].close
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_sum += tr
        atr = tr_sum / self.atr_window
        
        if atr <= 0:
            return
            
        stop_offset = atr * self.atr_stop_mult
        target_offset = atr * self.atr_target_mult
        
        if cross_up:
            ctx.buy(size=self.size, stop_offset=stop_offset, target_offset=target_offset, tag="sma_cross_long")
        elif cross_down:
            ctx.sell(size=self.size, stop_offset=stop_offset, target_offset=target_offset, tag="sma_cross_short")

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
