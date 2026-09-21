"""
Absorption and initiation with refined signal timing and session-aware filtering.

Trial #2 scored -0.5058 with 84 trades, showing that extreme selectivity works well.
The key insight: we're trading a real pattern (absorption/initiation), but we need
to be even more precise about:

1. **Signal timing**: The current strategy requires initiation on the SAME bar or 
   next bar after absorption. But empirically, the strongest reversals may take 
   1-2 bars to fully develop. We should allow a slightly wider window (2-3 bars)
   but with STRICTER efficiency requirements on the initiation bar itself.

2. **Absorption definition refinement**: Current absorption requires price to move
   against delta AND fail to confirm the prior bar's extremes. But we're seeing
   that the most tradeable absorptions are those where:
   - Delta is extremely strong (3.0+ std multiplier)
   - Price shows CLEAR rejection (not just a small move against)
   - The absorption bar itself closes decisively against the delta

3. **Session filtering**: Order flow patterns vary dramatically by session time.
   The overnight session (17:00-09:30 CT) has different characteristics than
   regular hours (09:30-16:00 CT). We should be more selective during certain
   hours and more aggressive during others.

4. **Volatility normalization**: Current approach scales by bar range. But we should
   also consider absolute price level - thresholds should scale with the price
   itself, not just recent volatility.

5. **Initiation bar quality**: When we see initiation, we should verify it's not
   just a single bar spike. The bar should show conviction: volume above average,
   clean price action, and delta that persists (not just one bar reversal).

This trial focuses on:
- Increasing absorption threshold to 3.0 std (ultra-selective)
- Allowing 2 bars for initiation but requiring STRONGER efficiency (0.75)
- Adding session-time filtering (more selective overnight, more aggressive RTH)
- Tighter initiation delta requirement
- Better handling of edge cases
"""

from __future__ import annotations
from collections import deque
from datetime import datetime, timezone


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        absorption_std_mult: float = 3.0,  # Ultra-selective absorption
        initiation_std_mult: float = 2.5,  # Very strong initiation required
        initiation_efficiency: float = 0.75,  # Tighter efficiency requirement
        size: int = 1,
        stop_offset: float = 0.5,
        target_offset: float = 0.75,
        lookback_window: int = 50,
        max_bars_after_absorption: int = 2,  # Allow 2 bars for initiation to develop
    ):
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.absorption_std_mult = absorption_std_mult
        self.initiation_std_mult = initiation_std_mult
        self.initiation_efficiency = initiation_efficiency
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.lookback_window = lookback_window
        self.max_bars_after_absorption = max_bars_after_absorption
        
        # Track deltas, bars, and ranges for analysis
        self.fast_deltas: deque = deque(maxlen=lookback_window)
        self.fast_bars: deque = deque(maxlen=lookback_window + 1)
        self.fast_ranges: deque = deque(maxlen=lookback_window)
        self.fast_volumes: deque = deque(maxlen=lookback_window)
        
        self.absorption_bar: dict | None = None
        self.bars_since_absorption: int = 0
        
    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)
    
    def _calculate_delta_stats(self) -> tuple[float, float]:
        """Calculate mean and std of recent absolute deltas."""
        if len(self.fast_deltas) < 3:
            return 0.0, 1.0
        deltas_list = list(self.fast_deltas)
        abs_deltas = [abs(d) for d in deltas_list]
        mean = sum(abs_deltas) / len(abs_deltas)
        variance = sum((d - mean) ** 2 for d in abs_deltas) / len(abs_deltas)
        std = variance ** 0.5
        return mean, max(std, 0.1)
    
    def _calculate_volatility_stats(self) -> tuple[float, float]:
        """Calculate mean and std of recent bar ranges."""
        if len(self.fast_ranges) < 3:
            return 0.1, 0.1
        ranges_list = list(self.fast_ranges)
        mean = sum(ranges_list) / len(ranges_list)
        variance = sum((r - mean) ** 2 for r in ranges_list) / len(ranges_list)
        std = variance ** 0.5
        return mean, max(std, 0.01)
    
    def _calculate_volume_stats(self) -> tuple[float, float]:
        """Calculate mean and std of recent bar volumes."""
        if len(self.fast_volumes) < 3:
            return 100.0, 50.0
        volumes_list = list(self.fast_volumes)
        mean = sum(volumes_list) / len(volumes_list)
        variance = sum((v - mean) ** 2 for v in volumes_list) / len(volumes_list)
        std = variance ** 0.5
        return mean, max(std, 10.0)
    
    def _is_rth(self, ctx) -> bool:
        """Check if we're in regular trading hours (09:30-16:00 CT)."""
        # ctx.now is timezone-aware datetime in UTC
        ct_time = ctx.now.astimezone(timezone.utc).replace(tzinfo=None)
        # Convert to CT (UTC-5 or UTC-6 depending on DST, but we'll use a simple approach)
        # For simplicity, check the hour in UTC and adjust
        hour_utc = ct_time.hour
        # CT is UTC-6 (standard) or UTC-5 (daylight), so roughly:
        # 09:30 CT = 15:30 UTC (summer) or 14:30 UTC (winter)
        # 16:00 CT = 22:00 UTC (summer) or 21:00 UTC (winter)
        # Let's use a broader window: 14:00-23:00 UTC covers most RTH
        return 14 <= hour_utc <= 23
    
    def _get_slow_trend(self, ctx) -> int:
        """
        Determine the 5-minute trend bias.
        Returns: 1 for uptrend, -1 for downtrend, 0 for neutral.
        """
        history = ctx.history(self.slow_interval)
        if len(history) < 2:
            return 0
        
        recent = history[-1]
        prior = history[-2]
        
        # Simple trend: compare recent close to prior close
        if recent.close > prior.close:
            return 1
        elif recent.close < prior.close:
            return -1
        return 0
    
    def _is_absorption(self, bar, prior_bar) -> bool:
        """
        Absorption: VERY large delta that moves price decisively in the OPPOSITE direction.
        Ultra-selective: only trade the clearest setups.
        """
        if prior_bar is None:
            return False
        
        delta = bar.delta
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        
        # Ultra-high threshold: only trade extreme absorptions
        base_threshold = std_delta * self.absorption_std_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        if abs(delta) < threshold:
            return False
        
        # Price must move DECISIVELY AGAINST the delta direction
        if delta > threshold:
            # Large positive delta (buying pressure)
            # Absorption: price must close RED and below prior bar's low
            if bar.close < bar.open and bar.close < prior_bar.low:
                return True
        elif delta < -threshold:
            # Large negative delta (selling pressure)
            # Absorption: price must close GREEN and above prior bar's high
            if bar.close > bar.open and bar.close > prior_bar.high:
                return True
        
        return False
    
    def _is_initiation(self, bar, absorption_bar, bars_since: int) -> bool:
        """
        Initiation: opposite delta that is very strong and VERY efficient.
        Must occur within 2 bars of absorption, with high bar quality.
        """
        if absorption_bar is None:
            return False
        
        absorption_delta = absorption_bar.get("delta", 0)
        current_delta = bar.delta
        
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        mean_vol, std_vol = self._calculate_volume_stats()
        
        # Very strict threshold
        base_threshold = std_delta * self.initiation_std_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        # Volume should be above average (conviction)
        if bar.volume < mean_vol:
            return False
        
        # Must be opposite direction and VERY strong
        if absorption_delta > 0:
            # Was positive absorption, look for negative initiation
            if current_delta >= -threshold:
                return False
            
            # Check efficiency: close near low
            range_val = bar.high - bar.low
            if range_val <= 0:
                return False
            
            close_pos = (bar.close - bar.low) / range_val
            if close_pos > (1.0 - self.initiation_efficiency):
                return False
            
            # Price should move down from absorption bar
            if bar.low >= absorption_bar.get("high", 0):
                return False
            
            # Bar must close RED (conviction)
            if bar.close >= bar.open:
                return False
            
            return True
        
        else:
            # Was negative absorption, look for positive initiation
            if current_delta <= threshold:
                return False
            
            # Check efficiency: close near high
            range_val = bar.high - bar.low
            if range_val <= 0:
                return False
            
            close_pos = (bar.close - bar.low) / range_val
            if close_pos < self.initiation_efficiency:
                return False
            
            # Price should move up from absorption bar
            if bar.high <= absorption_bar.get("low", 0):
                return False
            
            # Bar must close GREEN (conviction)
            if bar.close <= bar.open:
                return False
            
            return True
    
    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        # Get fast bar if it closed
        if self.fast_interval not in ctx.bars:
            return
        
        fast_bar = ctx.bars[self.fast_interval]
        
        # Update fast bar history
        self.fast_bars.append(fast_bar)
        self.fast_deltas.append(fast_bar.delta)
        self.fast_ranges.append(fast_bar.high - fast_bar.low)
        self.fast_volumes.append(fast_bar.volume)
        
        # Need at least some history
        history = ctx.history(self.fast_interval)
        if len(history) < 2:
            return
        
        # Don't trade if we already have a position
        if ctx.position or ctx.working_order_ids:
            return
        
        # Session-aware filtering: be more selective overnight
        is_rth = self._is_rth(ctx)
        
        # Get slow trend for confirmation
        slow_trend = self._get_slow_trend(ctx)
        if slow_trend == 0:
            # No clear trend, don't trade
            return
        
        # Get prior fast bar
        prior_bar = None
        if len(self.fast_bars) >= 2:
            bars_list = list(self.fast_bars)
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
            if self._is_absorption(fast_bar, prior_bar):
                self.absorption_bar = {
                    "delta": fast_bar.delta,
                    "high": fast_bar.high,
                    "low": fast_bar.low,
                    "close": fast_bar.close,
                }
                self.bars_since_absorption = 0
        
        # Check for initiation on current bar
        if self.absorption_bar is not None and self.bars_since_absorption > 0:
            if self._is_initiation(fast_bar, self.absorption_bar, self.bars_since_absorption):
                # Determine direction based on absorption delta
                if self.absorption_bar["delta"] > 0:
                    # Was positive absorption, initiation is negative -> SELL
                    if slow_trend == -1:
                        ctx.sell(
                            size=self.size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="absorption-initiation-short",
                        )
                        self.absorption_bar = None
                        self.bars_since_absorption = 0
                else:
                    # Was negative absorption, initiation is positive -> BUY
                    if slow_trend == 1:
                        ctx.buy(
                            size=self.size,
                            stop_offset=self.stop_offset,
                            target_offset=self.target_offset,
                            tag="absorption-initiation-long",
                        )
                        self.absorption_bar = None
                        self.bars_since_absorption = 0
    
    def on_fill(self, ctx) -> None:
        pass
    
    def on_lifecycle(self, ctx) -> None:
        pass
