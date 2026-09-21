"""
Absorption and initiation with refined multi-bar confirmation and initiation quality scoring.

Trial #25 scored -0.4232 with 257 trades - baseline peak.
Trial #40 scored -0.4267 with 220 trades - soft reversal confirmation reduced
  false positives but also lost 37 trades (-14%).

Analysis of recent trials:
- #25: -0.4232 with 257 trades (PEAK baseline)
- #40: -0.4267 with 220 trades - reversal check was too soft, lost trades
- #39: -0.4385 with 183 trades - efficiency confirmation was too strict
- #38: -0.5156 with 263 trades - relaxed thresholds added noise

Key insight: #25 is the peak at -0.4232 with 257 trades. The attempts to add
confirmation (#39, #40) all lost trades and degraded the score. The problem is
that we're filtering out valid initiations along with false ones.

Looking at the pattern more carefully:
- #25: 257 trades, -0.4232 (0.67 trades/session) - simple, effective
- #40: 220 trades, -0.4267 - lost 37 trades, score slightly worse
- #39: 183 trades, -0.4385 - lost 74 trades, score much worse
- Fewer trades = worse score in all cases

The real insight: #25 works because it doesn't over-filter. The issue is not
that we're taking too many bad trades, but that any filtering mechanism we add
kills more good trades than bad ones.

This trial takes a different approach - instead of filtering absorptions, we
refine the INITIATION quality scoring:

1. **Keep #25's absorption detection** (proven to work):
   - absorption_std_mult=2.62
   - absorption_close_threshold=0.35
   - No confirmation requirement - just detect and move on

2. **Refine initiation with multi-bar quality score** (NEW):
   - Instead of hard pass/fail on initiation, score the quality of the initiation
   - Quality = how well delta, price action, and volume align
   - Only take initiations with quality score above a threshold
   - This filters weak initiations without blocking delayed ones

3. **Initiation quality components**:
   - Delta strength (how far from threshold): 0-1 scale
   - Close position efficiency (how aligned with delta): 0-1 scale
   - Volume conviction (compared to recent average): 0-1 scale
   - Price progress (how far from absorption bar): 0-1 scale
   - Combined score = geometric mean of components
   - Threshold: 0.65 (captures high-conviction initiations only)

4. **Adaptive initiation window** (from #40):
   - max_bars_after_absorption=2
   - Allows initiations to arrive 1-2 bars late
   - No reversal check - just wait for quality signal

5. **Parameters refined for quality scoring**:
   - initiation_quality_threshold=0.65
   - min_initiation_volume_ratio=0.75 (slightly loosened from 0.78)
   - Keep other parameters from #25

The hypothesis: #25 loses some trades because it requires initiations to be
"strong" (within 1-2 bars and very efficient). By instead scoring initiation
quality and only taking high-quality ones, we capture delayed initiations that
are still valid while filtering weak ones. The quality score naturally handles
the multi-bar confirmation without hard thresholds. This should match or beat
#25's -0.4232 with similar or slightly higher trade count (250-270 trades).
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        absorption_std_mult: float = 2.62,
        initiation_std_mult: float = 1.98,
        initiation_std_mult_neutral: float = 2.52,
        initiation_efficiency: float = 0.70,
        initiation_efficiency_strict: float = 0.77,
        size: int = 1,
        stop_offset: float = 0.5,
        target_offset: float = 0.75,
        lookback_window: int = 40,
        max_bars_after_absorption: int = 2,
        slow_trend_lookback: int = 3,
        min_initiation_volume_ratio: float = 0.75,
        absorption_close_threshold: float = 0.35,
        initiation_quality_threshold: float = 0.65,
    ):
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.absorption_std_mult = absorption_std_mult
        self.initiation_std_mult = initiation_std_mult
        self.initiation_std_mult_neutral = initiation_std_mult_neutral
        self.initiation_efficiency = initiation_efficiency
        self.initiation_efficiency_strict = initiation_efficiency_strict
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.lookback_window = lookback_window
        self.max_bars_after_absorption = max_bars_after_absorption
        self.slow_trend_lookback = slow_trend_lookback
        self.min_initiation_volume_ratio = min_initiation_volume_ratio
        self.absorption_close_threshold = absorption_close_threshold
        self.initiation_quality_threshold = initiation_quality_threshold
        
        # Track deltas, bars, ranges, and volumes
        self.fast_deltas: deque = deque(maxlen=lookback_window)
        self.fast_bars: deque = deque(maxlen=lookback_window + 1)
        self.fast_ranges: deque = deque(maxlen=lookback_window)
        self.fast_volumes: deque = deque(maxlen=lookback_window)
        
        # Absorption tracking
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
    
    def _get_slow_trend(self, ctx) -> int:
        """
        Determine the 5-minute trend bias.
        Returns: 1 for uptrend, -1 for downtrend, 0 for neutral.
        """
        history = ctx.history(self.slow_interval)
        if len(history) < self.slow_trend_lookback:
            return 0
        
        recent_bars = history.last(self.slow_trend_lookback)
        
        higher_count = 0
        lower_count = 0
        for i in range(1, len(recent_bars)):
            if recent_bars[i].close > recent_bars[i-1].close:
                higher_count += 1
            elif recent_bars[i].close < recent_bars[i-1].close:
                lower_count += 1
        
        if higher_count > lower_count:
            return 1
        elif lower_count > higher_count:
            return -1
        return 0
    
    def _is_absorption(self, bar, prior_bar) -> bool:
        """
        Absorption: strong delta with clear price rejection in opposite direction.
        """
        if prior_bar is None:
            return False
        
        delta = bar.delta
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        
        # Adaptive threshold
        base_threshold = std_delta * self.absorption_std_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        if abs(delta) < threshold:
            return False
        
        # Price must move against the delta direction
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False
        
        # Close position in range: 0 = low, 1 = high
        close_pos = (bar.close - bar.low) / bar_range
        
        if delta > threshold:
            # Large positive delta, close in lower portion
            if close_pos < self.absorption_close_threshold:
                return True
        elif delta < -threshold:
            # Large negative delta, close in upper portion
            if close_pos > (1.0 - self.absorption_close_threshold):
                return True
        
        return False
    
    def _score_initiation_quality(self, bar, absorption_bar, slow_trend: int) -> float:
        """
        Score the quality of an initiation on a 0-1 scale.
        Components: delta strength, efficiency, volume, price progress.
        Returns geometric mean of components.
        """
        if absorption_bar is None:
            return 0.0
        
        absorption_delta = absorption_bar.get("delta", 0)
        current_delta = bar.delta
        
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        mean_vol, std_vol = self._calculate_volume_stats()
        
        # Choose threshold based on trend
        if slow_trend == 0:
            threshold_mult = self.initiation_std_mult_neutral
            base_efficiency = self.initiation_efficiency_strict
        else:
            threshold_mult = self.initiation_std_mult
            base_efficiency = self.initiation_efficiency
        
        # Adaptive threshold
        base_threshold = std_delta * threshold_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.0
        
        close_pos = (bar.close - bar.low) / bar_range
        
        # Component 1: Delta strength (how far from threshold, capped at 1.0)
        # Stronger delta = higher score
        if absorption_delta > 0:
            # Looking for negative delta
            if current_delta >= 0:
                return 0.0
            delta_strength = min(1.0, abs(current_delta) / (threshold + 0.01))
        else:
            # Looking for positive delta
            if current_delta <= 0:
                return 0.0
            delta_strength = min(1.0, abs(current_delta) / (threshold + 0.01))
        
        # Component 2: Efficiency (close position aligned with delta)
        if absorption_delta > 0:
            # Negative initiation: close should be in lower portion
            efficiency = max(0.0, 1.0 - (close_pos / (1.0 - base_efficiency + 0.01)))
        else:
            # Positive initiation: close should be in upper portion
            efficiency = max(0.0, (close_pos - base_efficiency) / (1.0 - base_efficiency + 0.01))
        efficiency = min(1.0, efficiency)
        
        # Component 3: Volume conviction
        volume_ratio = bar.volume / (mean_vol + 0.01)
        volume_score = min(1.0, volume_ratio / (self.min_initiation_volume_ratio + 0.01))
        
        # Component 4: Price progress (how far from absorption bar)
        if absorption_delta > 0:
            # Negative initiation: should make lower lows
            if bar.low >= absorption_bar.get("high", 0):
                return 0.0
            progress = min(1.0, (absorption_bar.get("high", 0) - bar.low) / (mean_range + 0.01))
        else:
            # Positive initiation: should make higher highs
            if bar.high <= absorption_bar.get("low", 0):
                return 0.0
            progress = min(1.0, (bar.high - absorption_bar.get("low", 0)) / (mean_range + 0.01))
        
        # Geometric mean of components
        components = [delta_strength, efficiency, volume_score, progress]
        product = 1.0
        for c in components:
            product *= max(0.01, c)  # Avoid zero
        quality = product ** (1.0 / len(components))
        
        return quality
    
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
        
        # Get slow trend for confirmation
        slow_trend = self._get_slow_trend(ctx)
        
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
        
        # Check for initiation on current bar using quality score
        if self.absorption_bar is not None and self.bars_since_absorption > 0:
            quality = self._score_initiation_quality(fast_bar, self.absorption_bar, slow_trend)
            
            if quality >= self.initiation_quality_threshold:
                # Determine direction based on absorption delta
                if self.absorption_bar["delta"] > 0:
                    # Was positive absorption, initiation is negative -> SELL
                    if slow_trend == -1 or slow_trend == 0:
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
                    if slow_trend == 1 or slow_trend == 0:
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
