"""
Refined absorption and initiation strategy with improved signal detection.

Previous trial (#0) scored -0.8425 with 6722 trades, indicating the strategy
was over-trading and likely losing on commission. Key improvements:

1. More selective absorption detection: require price to move in the *opposite*
   direction of delta, not just fail to confirm. This filters out ambiguous bars.

2. Stricter initiation criteria: require stronger delta reversal and tighter
   price action alignment with the new direction.

3. Longer absorption window: allow initiation up to 3-4 bars after absorption
   to catch delayed reversals, but reset more aggressively on conflicting signals.

4. Better normalization: use absolute delta magnitude against recent volatility,
   not just standard deviation of signed deltas.

5. Reduce trade frequency: increase thresholds to trade only the highest-conviction
   setups, reducing commission drag from 6722 trades.

The core hypothesis remains: absorption (delta-price disagreement) marks a turn,
especially when followed by efficient flow in the opposite direction.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "1m",
        absorption_std_mult: float = 2.0,  # Increased from 1.5
        initiation_std_mult: float = 1.5,  # Increased from 1.0
        initiation_efficiency: float = 0.65,  # Tighter efficiency requirement
        size: int = 1,
        stop_offset: float = 0.5,
        target_offset: float = 0.75,
        lookback_window: int = 30,  # Increased for better stats
        max_bars_after_absorption: int = 3,  # Allow 3 bars for initiation
    ):
        self.interval = interval
        self.absorption_std_mult = absorption_std_mult
        self.initiation_std_mult = initiation_std_mult
        self.initiation_efficiency = initiation_efficiency
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.lookback_window = lookback_window
        self.max_bars_after_absorption = max_bars_after_absorption
        
        # Track deltas and bars for analysis
        self.deltas: deque = deque(maxlen=lookback_window)
        self.bars: deque = deque(maxlen=lookback_window + 1)
        self.absorption_bar: dict | None = None
        self.bars_since_absorption: int = 0
        
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
    
    def _calculate_delta_stats(self) -> tuple[float, float]:
        """Calculate mean and std of recent absolute deltas."""
        if len(self.deltas) < 3:
            return 0.0, 1.0
        deltas_list = list(self.deltas)
        abs_deltas = [abs(d) for d in deltas_list]
        mean = sum(abs_deltas) / len(abs_deltas)
        variance = sum((d - mean) ** 2 for d in abs_deltas) / len(abs_deltas)
        std = variance ** 0.5
        return mean, max(std, 0.1)
    
    def _is_absorption(self, bar, prior_bar) -> bool:
        """
        Absorption: large delta that moves price in the OPPOSITE direction.
        
        This is stricter than just "price fails to confirm" - we want actual
        disagreement that suggests order flow was absorbed/resisted.
        """
        if prior_bar is None:
            return False
        
        delta = bar.delta
        mean_delta, std_delta = self._calculate_delta_stats()
        
        # Threshold for "large" delta - increased threshold
        threshold = std_delta * self.absorption_std_mult
        
        if abs(delta) < threshold:
            return False
        
        # Price must move AGAINST the delta direction
        if delta > threshold:
            # Large positive delta (buying pressure)
            # Absorption: price moves DOWN despite the buying
            if bar.close < prior_bar.close and bar.low < prior_bar.low:
                return True
        elif delta < -threshold:
            # Large negative delta (selling pressure)
            # Absorption: price moves UP despite the selling
            if bar.close > prior_bar.close and bar.high > prior_bar.high:
                return True
        
        return False
    
    def _is_initiation(self, bar, absorption_bar) -> bool:
        """
        Initiation: opposite delta that is strong and efficient (moves price cleanly).
        
        The reversal must be both strong in order flow AND clean in price action.
        """
        if absorption_bar is None:
            return False
        
        absorption_delta = absorption_bar.get("delta", 0)
        current_delta = bar.delta
        
        mean_delta, std_delta = self._calculate_delta_stats()
        threshold = std_delta * self.initiation_std_mult
        
        # Must be opposite direction and strong
        if absorption_delta > 0:
            # Was positive absorption (buying was absorbed downward)
            # Look for negative initiation (selling that moves down)
            if current_delta >= -threshold:
                return False
            
            # Check efficiency: price must move down, close near low
            range_val = bar.high - bar.low
            if range_val <= 0:
                return False
            
            close_pos = (bar.close - bar.low) / range_val
            # Close should be in lower portion (near low)
            if close_pos > (1.0 - self.initiation_efficiency):
                return False
            
            # Price should actually move down from prior absorption bar
            if bar.low >= absorption_bar.get("high", 0):
                return False
            
            return True
        
        else:
            # Was negative absorption (selling was absorbed upward)
            # Look for positive initiation (buying that moves up)
            if current_delta <= threshold:
                return False
            
            # Check efficiency: price must move up, close near high
            range_val = bar.high - bar.low
            if range_val <= 0:
                return False
            
            close_pos = (bar.close - bar.low) / range_val
            # Close should be in upper portion (near high)
            if close_pos < self.initiation_efficiency:
                return False
            
            # Price should actually move up from prior absorption bar
            if bar.high <= absorption_bar.get("low", 0):
                return False
            
            return True
    
    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Get current bar
        if self.interval not in ctx.bars:
            return
        
        bar = ctx.bars[self.interval]
        
        # Update history
        self.bars.append(bar)
        self.deltas.append(bar.delta)
        
        # Need at least some history
        history = ctx.history(self.interval)
        if len(history) < 2:
            return
        
        # Don't trade if we already have a position
        if ctx.position or ctx.working_order_ids:
            return
        
        # Get prior bar
        prior_bar = None
        if len(self.bars) >= 2:
            bars_list = list(self.bars)
            prior_bar = bars_list[-2]
        
        # Update bars_since_absorption counter
        if self.absorption_bar is not None:
            self.bars_since_absorption += 1
            
            # Reset if too much time has passed
            if self.bars_since_absorption > self.max_bars_after_absorption:
                self.absorption_bar = None
                self.bars_since_absorption = 0
        
        # Check for absorption on the current bar
        if prior_bar is not None and self.absorption_bar is None:
            if self._is_absorption(bar, prior_bar):
                self.absorption_bar = {
                    "delta": bar.delta,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                }
                self.bars_since_absorption = 0
        
        # Check for initiation on current bar
        if self.absorption_bar is not None and self.bars_since_absorption > 0:
            if self._is_initiation(bar, self.absorption_bar):
                # Determine direction based on absorption delta
                if self.absorption_bar["delta"] > 0:
                    # Was positive absorption, initiation is negative -> SELL
                    ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="absorption-initiation-short",
                    )
                else:
                    # Was negative absorption, initiation is positive -> BUY
                    ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag="absorption-initiation-long",
                    )
                
                # Reset after trade
                self.absorption_bar = None
                self.bars_since_absorption = 0
    
    def on_fill(self, ctx) -> None:
        pass
    
    def on_lifecycle(self, ctx) -> None:
        pass
