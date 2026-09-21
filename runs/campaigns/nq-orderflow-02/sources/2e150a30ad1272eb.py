"""
Absorption and initiation strategy for order flow analysis.

The hypothesis: absorption (delta and price disagreeing) marks a turn, especially
when followed by initiation (efficient flow in the opposite direction).

Strategy:
1. On 1-minute bars, measure absorption: large delta that fails to move price.
2. Normalize delta against recent range to handle session volatility.
3. Look for initiation: opposite delta that is efficient (closes near extreme).
4. Enter on initiation following absorption, with brackets for protection.

Key parameters:
- absorption_std_mult: delta must exceed this many standard deviations
- price_failure_bars: how many prior bars to check for price disagreement
- initiation_std_mult: how strong the opposite delta must be
- initiation_efficiency: how close to the extreme the close must be
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "1m",
        absorption_std_mult: float = 1.5,
        price_failure_bars: int = 1,
        initiation_std_mult: float = 1.0,
        initiation_efficiency: float = 0.7,
        size: int = 1,
        stop_offset: float = 0.5,
        target_offset: float = 0.75,
        lookback_window: int = 20,
    ):
        self.interval = interval
        self.absorption_std_mult = absorption_std_mult
        self.price_failure_bars = price_failure_bars
        self.initiation_std_mult = initiation_std_mult
        self.initiation_efficiency = initiation_efficiency
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.lookback_window = lookback_window
        
        # Track deltas and bars for analysis
        self.deltas: deque = deque(maxlen=lookback_window)
        self.bars: deque = deque(maxlen=lookback_window + 1)
        self.absorption_bar_idx: int | None = None  # Track when absorption occurred
        
    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
    
    def _calculate_delta_stats(self) -> tuple[float, float]:
        """Calculate mean and std of recent deltas."""
        if len(self.deltas) < 2:
            return 0.0, 1.0
        deltas_list = list(self.deltas)
        mean = sum(deltas_list) / len(deltas_list)
        variance = sum((d - mean) ** 2 for d in deltas_list) / len(deltas_list)
        std = variance ** 0.5
        return mean, max(std, 0.01)  # Avoid division by zero
    
    def _is_absorption(self, bar, prior_bars) -> bool:
        """
        Check if current bar shows absorption:
        - Large delta in one direction
        - Price fails to confirm the delta direction
        """
        if len(prior_bars) < self.price_failure_bars:
            return False
        
        delta = bar.delta
        mean_delta, std_delta = self._calculate_delta_stats()
        
        # Threshold for "large" delta
        threshold = std_delta * self.absorption_std_mult
        
        if abs(delta) < threshold:
            return False
        
        # Check price failure
        if delta > threshold:
            # Large positive delta (buying pressure)
            # Check if price failed to go higher
            prior = prior_bars[-1]
            if bar.high < prior.high or bar.close < prior.close:
                return True
        elif delta < -threshold:
            # Large negative delta (selling pressure)
            # Check if price failed to go lower
            prior = prior_bars[-1]
            if bar.low > prior.low or bar.close > prior.close:
                return True
        
        return False
    
    def _is_initiation(self, bar, prior_absorption_bar) -> bool:
        """
        Check if current bar shows initiation:
        - Delta in opposite direction to absorption
        - Delta is strong enough
        - Price moves efficiently (closes near extreme)
        """
        if prior_absorption_bar is None:
            return False
        
        absorption_delta = prior_absorption_bar.delta
        current_delta = bar.delta
        
        mean_delta, std_delta = self._calculate_delta_stats()
        threshold = std_delta * self.initiation_std_mult
        
        # Must be opposite direction and strong
        if absorption_delta > 0:
            # Was positive absorption, look for negative initiation
            if current_delta >= -threshold:
                return False
            # Check efficiency: close near low
            range_val = bar.high - bar.low
            if range_val > 0:
                close_pos = (bar.close - bar.low) / range_val
                if close_pos > (1.0 - self.initiation_efficiency):
                    return False
            return True
        else:
            # Was negative absorption, look for positive initiation
            if current_delta <= threshold:
                return False
            # Check efficiency: close near high
            range_val = bar.high - bar.low
            if range_val > 0:
                close_pos = (bar.close - bar.low) / range_val
                if close_pos < self.initiation_efficiency:
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
        
        # Convert deque to list for easier indexing
        prior_bars = list(self.bars)[:-1]  # All except current
        
        # Check for absorption on the previous bar
        if len(prior_bars) >= 1:
            prev_bar = prior_bars[-1]
            if self._is_absorption(prev_bar, prior_bars[:-1]):
                self.absorption_bar_idx = len(self.bars) - 2
        
        # Check for initiation on current bar
        if self.absorption_bar_idx is not None:
            bars_since_absorption = len(self.bars) - 1 - self.absorption_bar_idx
            # Allow initiation up to 2 bars after absorption
            if bars_since_absorption <= 2:
                absorption_bar = self.bars[self.absorption_bar_idx]
                if self._is_initiation(bar, absorption_bar):
                    # Determine direction based on absorption
                    if absorption_bar.delta > 0:
                        # Was positive absorption, initiation is negative -> sell
                        ctx.sell(
                            size=self.size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="absorption-initiation-short",
                        )
                        self.absorption_bar_idx = None
                    else:
                        # Was negative absorption, initiation is positive -> buy
                        ctx.buy(
                            size=self.size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="absorption-initiation-long",
                        )
                        self.absorption_bar_idx = None
            elif bars_since_absorption > 2:
                # Reset if too much time has passed
                self.absorption_bar_idx = None
    
    def on_fill(self, ctx) -> None:
        pass
    
    def on_lifecycle(self, ctx) -> None:
        pass
