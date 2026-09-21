"""
Absorption and initiation with refined multi-bar absorption confirmation and
relaxed initiation thresholds.

Trial #25 scored -0.4232 with 257 trades - best absolute score.
Trial #37 scored -0.5106 with 198 trades - multi-bar confirmation helped but
  initiation thresholds still too strict.

Analysis of trial progression:
- #25: -0.4232 with 257 trades (PEAK baseline, simple approach)
- #37: -0.5106 (198 trades) - multi-bar confirmation reduced false absorptions
  but cost us trades (198 vs 257)
- #36: -0.4792 (106 trades) - initiation_delta_ratio too constraining
- #35: -0.5140 (102 trades) - even stricter requirements

Key insight: #25 works because it generates 257 trades with good quality. #37
improved absorption filtering but lost 59 trades (-23%) and score degraded.
The multi-bar confirmation is good for reducing false absorptions, but we're
being too strict on initiation.

The real problem: we're filtering out valid initiations. Looking at the pattern:
- #25: 257 trades, -0.4232 (0.67 trades/session)
- #37: 198 trades, -0.5106 (0.52 trades/session)

We lost 59 trades and the score got worse. This suggests the confirmation was
too strict. The solution: keep multi-bar absorption confirmation (it's good)
but RELAX initiation thresholds.

This trial refines #37 with:

1. **Keep #37's multi-bar absorption confirmation**:
   - State machine: candidate -> confirmed -> initiation window
   - Filters false one-bar spikes effectively
   - Reduces noise in absorption detection

2. **Relax initiation thresholds** (KEY CHANGE):
   - Reduce initiation_std_mult from 1.98 to 1.75
   - Reduce initiation_std_mult_neutral from 2.52 to 2.20
   - Reduce initiation_efficiency from 0.70 to 0.65
   - Reduce initiation_efficiency_strict from 0.77 to 0.72
   - These are less strict but still meaningful thresholds
   - Should recover some of the 59 lost trades

3. **Slightly tighter absorption detection** (REFINEMENT):
   - Keep absorption_std_mult at 2.62
   - Keep absorption_close_threshold at 0.35
   - Keep absorption_volume_ratio at 0.80
   - These are good filters for false absorptions

4. **Absorption persistence** (from #25):
   - max_bars_after_absorption=2 (allow 1 bar delay)
   - This matches #25's proven window

The hypothesis: #37's multi-bar confirmation is good, but we over-corrected
by tightening initiation. By relaxing initiation thresholds while keeping the
good absorption filtering, we should get closer to #25's trade count (240-250
trades) while maintaining the lower false-positive rate. Target: -0.4250 to
-0.4300 with 240-255 trades.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        absorption_std_mult: float = 2.62,
        initiation_std_mult: float = 1.75,
        initiation_std_mult_neutral: float = 2.20,
        initiation_efficiency: float = 0.65,
        initiation_efficiency_strict: float = 0.72,
        size: int = 1,
        stop_offset: float = 0.5,
        target_offset: float = 0.75,
        lookback_window: int = 40,
        max_bars_after_absorption: int = 2,
        slow_trend_lookback: int = 3,
        min_initiation_volume_ratio: float = 0.78,
        absorption_close_threshold: float = 0.35,
        absorption_volume_ratio: float = 0.80,
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
        self.absorption_volume_ratio = absorption_volume_ratio
        
        # Track deltas, bars, ranges, and volumes
        self.fast_deltas: deque = deque(maxlen=lookback_window)
        self.fast_bars: deque = deque(maxlen=lookback_window + 1)
        self.fast_ranges: deque = deque(maxlen=lookback_window)
        self.fast_volumes: deque = deque(maxlen=lookback_window)
        
        # Absorption state machine
        # 0: no absorption
        # 1: absorption candidate (strong delta + price rejection)
        # 2: absorption confirmed (candidate + confirmation bar)
        self.absorption_state: int = 0
        self.absorption_bar: dict | None = None
        self.absorption_candidate: dict | None = None
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
    
    def _is_absorption_candidate(self, bar, prior_bar) -> bool:
        """
        Check if bar shows absorption pattern:
        strong delta with price rejection in opposite direction.
        """
        if prior_bar is None:
            return False
        
        delta = bar.delta
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        mean_vol, std_vol = self._calculate_volume_stats()
        
        # Volume check: absorption should have conviction
        if bar.volume < mean_vol * self.absorption_volume_ratio:
            return False
        
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
            # Large positive delta (buying pressure)
            # Absorption: close in lower portion showing rejection
            if close_pos < self.absorption_close_threshold:
                return True
        elif delta < -threshold:
            # Large negative delta (selling pressure)
            # Absorption: close in upper portion showing rejection
            if close_pos > (1.0 - self.absorption_close_threshold):
                return True
        
        return False
    
    def _confirms_absorption(self, bar, absorption_candidate) -> bool:
        """
        Check if current bar confirms the absorption candidate.
        Confirmation happens if:
        - Volume is lower (rejection held), OR
        - Close position is further in rejection direction
        """
        if absorption_candidate is None:
            return False
        
        cand_delta = absorption_candidate.get("delta", 0)
        cand_close_pos = absorption_candidate.get("close_pos", 0.5)
        cand_volume = absorption_candidate.get("volume", 0)
        
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False
        close_pos = (bar.close - bar.low) / bar_range
        
        # Check volume confirmation
        if bar.volume < cand_volume:
            return True
        
        # Check directional confirmation
        if cand_delta > 0:
            # Positive absorption (rejected low), confirm if even lower close
            if close_pos < cand_close_pos:
                return True
        else:
            # Negative absorption (rejected high), confirm if even higher close
            if close_pos > cand_close_pos:
                return True
        
        return False
    
    def _is_initiation(self, bar, absorption_bar, slow_trend: int) -> bool:
        """
        Initiation: opposite delta that is strong and efficient.
        """
        if absorption_bar is None:
            return False
        
        absorption_delta = absorption_bar.get("delta", 0)
        current_delta = bar.delta
        
        mean_delta, std_delta = self._calculate_delta_stats()
        mean_range, std_range = self._calculate_volatility_stats()
        mean_vol, std_vol = self._calculate_volume_stats()
        
        # Choose threshold and base efficiency based on trend
        if slow_trend == 0:
            # Neutral trend: require stricter initiation
            threshold_mult = self.initiation_std_mult_neutral
            base_efficiency = self.initiation_efficiency_strict
        else:
            # Have trend: use normal threshold
            threshold_mult = self.initiation_std_mult
            base_efficiency = self.initiation_efficiency
        
        # Adaptive threshold
        base_threshold = std_delta * threshold_mult
        range_factor = max(0.8, min(1.2, (bar.high - bar.low) / (mean_range + 0.01)))
        threshold = base_threshold / range_factor
        
        # Volume check: reasonable conviction
        if bar.volume < mean_vol * self.min_initiation_volume_ratio:
            return False
        
        # Close position in range
        range_val = bar.high - bar.low
        if range_val <= 0:
            return False
        close_pos = (bar.close - bar.low) / range_val
        
        # Must be opposite direction and strong enough
        if absorption_delta > 0:
            # Was positive absorption, look for negative initiation
            if current_delta >= -threshold:
                return False
            
            # Check efficiency: close in lower portion
            if close_pos > (1.0 - base_efficiency):
                return False
            
            # Price should make progress downward
            if bar.low >= absorption_bar.get("high", 0):
                return False
            
            return True
        
        else:
            # Was negative absorption, look for positive initiation
            if current_delta <= threshold:
                return False
            
            # Check efficiency: close in upper portion
            if close_pos < base_efficiency:
                return False
            
            # Price should make progress upward
            if bar.high <= absorption_bar.get("low", 0):
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
                self.absorption_state = 0
                self.bars_since_absorption = 0
        
        # State machine for absorption confirmation
        if self.absorption_state == 0:
            # No absorption - check for candidate
            if prior_bar is not None:
                if self._is_absorption_candidate(fast_bar, prior_bar):
                    bar_range = fast_bar.high - fast_bar.low
                    if bar_range > 0:
                        close_pos = (fast_bar.close - fast_bar.low) / bar_range
                        self.absorption_candidate = {
                            "delta": fast_bar.delta,
                            "high": fast_bar.high,
                            "low": fast_bar.low,
                            "close": fast_bar.close,
                            "volume": fast_bar.volume,
                            "close_pos": close_pos,
                        }
                        self.absorption_state = 1
        
        elif self.absorption_state == 1:
            # Absorption candidate - check for confirmation
            if self.absorption_candidate is not None:
                if self._confirms_absorption(fast_bar, self.absorption_candidate):
                    # Confirmed! Move to state 2
                    self.absorption_bar = self.absorption_candidate
                    self.absorption_state = 2
                    self.bars_since_absorption = 0
                    self.absorption_candidate = None
                else:
                    # Not confirmed, reset
                    self.absorption_candidate = None
                    self.absorption_state = 0
        
        elif self.absorption_state == 2:
            # Absorption confirmed - check for initiation
            if self.absorption_bar is not None:
                if self._is_initiation(fast_bar, self.absorption_bar, slow_trend):
                    # Determine direction based on absorption delta
                    if self.absorption_bar["delta"] > 0:
                        # Was positive absorption, initiation is negative -> SELL
                        # Enter if downtrend OR neutral with very strong initiation
                        if slow_trend == -1 or slow_trend == 0:
                            ctx.sell(
                                size=self.size,
                                stop_offset=self.stop_offset,
                                target_offset=self.target_offset,
                                tag="absorption-initiation-short",
                            )
                            self.absorption_bar = None
                            self.absorption_state = 0
                            self.bars_since_absorption = 0
                    else:
                        # Was negative absorption, initiation is positive -> BUY
                        # Enter if uptrend OR neutral with very strong initiation
                        if slow_trend == 1 or slow_trend == 0:
                            ctx.buy(
                                size=self.size,
                                stop_offset=self.stop_offset,
                                target_offset=self.target_offset,
                                tag="absorption-initiation-long",
                            )
                            self.absorption_bar = None
                            self.absorption_state = 0
                            self.bars_since_absorption = 0
    
    def on_fill(self, ctx) -> None:
        pass
    
    def on_lifecycle(self, ctx) -> None:
        pass
