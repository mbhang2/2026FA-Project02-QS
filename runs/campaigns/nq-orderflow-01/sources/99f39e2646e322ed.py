"""
Absorption and initiation strategy with dual-timeframe confirmation and
adaptive positioning.

Analysis of trial progression shows a critical insight:
- Trial #6: -0.1967 (158 trades) - Best baseline
- Trial #22: -0.2182 (176 trades) - Regression despite more trades
- Trial #21: -0.2026 (157 trades) - Good but slightly worse than #6
- Trial #20: -0.2083 (149 trades) - Regression

The pattern is clear: the best scores cluster around 150-160 trades with -0.19 to -0.21.
Trials with >170 trades consistently underperform, suggesting over-trading reduces quality.

Key insight from #22's regression: requiring initiation delta to be MORE extreme
than absorption delta (bar.delta < self.absorption_delta for sells) was too strict.
This killed high-quality reversals where the initiation bar shows strong opposite
flow but not necessarily MORE extreme than the absorption bar.

New approach for this trial:
1. Return to simpler initiation logic: just require opposite delta above threshold
2. Add 5m bar filter: only enter if 5m bar is trending in our intended direction
3. This provides multi-timeframe confirmation without over-constraining 1m signal
4. For buy absorption (want to sell): 5m should be trending down or neutral
5. For sell absorption (want to buy): 5m should be trending up or neutral
6. Keep proven parameters from #6:
   - stop_offset: 0.40
   - target_offset: 0.65
   - max_hold_bars: 6
   - delta_percentile: 0.75
   - volume_threshold: 0.60
7. Adjust initiation window to 3 (give more time for 5m confirmation)
8. Use simpler initiation delta check: 0.70 of threshold (no extremity requirement)

Expected outcome: ~155-165 trades with score around -0.1975 by:
- Multi-timeframe confirmation filters false signals
- Simpler 1m logic catches more valid reversals
- 5m filter adds robustness without killing signal
- Returns to proven parameter set from #6
"""

from collections import deque
from typing import Deque, Optional


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        lookback: int = 30,
        delta_percentile: float = 0.75,
        volume_threshold: float = 0.60,
        stop_offset: float = 0.40,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 6,
        initiation_window: int = 3,
        initiation_delta_strength: float = 0.70,
    ) -> None:
        self.fast_interval: str = fast_interval
        self.slow_interval: str = slow_interval
        self.lookback: int = lookback
        self.delta_percentile: float = delta_percentile
        self.volume_threshold: float = volume_threshold
        self.stop_offset: float = stop_offset
        self.target_offset: float = target_offset
        self.size: int = size
        self.max_hold_bars: int = max_hold_bars
        self.initiation_window: int = initiation_window
        self.initiation_delta_strength: float = initiation_delta_strength

        # Fast (1m) history
        self.fast_delta_history: Deque[int] = deque(maxlen=lookback)
        self.fast_volume_history: Deque[int] = deque(maxlen=lookback)
        self.fast_high_history: Deque[float] = deque(maxlen=lookback)
        self.fast_low_history: Deque[float] = deque(maxlen=lookback)
        self.fast_close_history: Deque[float] = deque(maxlen=lookback)

        # Slow (5m) history for confirmation
        self.slow_delta_history: Deque[int] = deque(maxlen=lookback)
        self.slow_close_history: Deque[float] = deque(maxlen=lookback)
        self.slow_open_history: Deque[float] = deque(maxlen=lookback)

        # Signal tracking
        self.absorption_signal: Optional[str] = None
        self.bars_since_absorption: int = 0
        self.bars_held: int = 0
        self.absorption_high: float = 0.0
        self.absorption_low: float = 0.0
        self.absorption_midpoint: float = 0.0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Get fast bars
        fast_history = ctx.history(self.fast_interval)
        if len(fast_history) < 1:
            return

        fast_bar = fast_history[-1]

        # Update fast history
        self.fast_delta_history.append(fast_bar.delta)
        self.fast_volume_history.append(fast_bar.volume)
        self.fast_high_history.append(fast_bar.high)
        self.fast_low_history.append(fast_bar.low)
        self.fast_close_history.append(fast_bar.close)

        # Update slow history if available
        slow_history = ctx.history(self.slow_interval)
        if len(slow_history) >= 1:
            slow_bar = slow_history[-1]
            self.slow_delta_history.append(slow_bar.delta)
            self.slow_close_history.append(slow_bar.close)
            self.slow_open_history.append(slow_bar.open)

        # Position management
        if ctx.position:
            self.bars_held += 1
            # Exit after max hold period
            if self.bars_held >= self.max_hold_bars:
                ctx.close()
            self.absorption_signal = None
            self.bars_since_absorption = 0
            return

        # Reset when flat
        self.bars_held = 0

        # Detect absorption on fast bars
        absorption = self._detect_absorption(fast_history)

        if absorption:
            self.absorption_signal = absorption
            self.bars_since_absorption = 0
            # Store absorption bar's high/low for midpoint calculation
            if len(fast_history) >= 1:
                self.absorption_high = fast_history[-1].high
                self.absorption_low = fast_history[-1].low
                self.absorption_midpoint = (self.absorption_high + self.absorption_low) / 2.0
            return

        # Check for initiation after absorption (within the window)
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            if self.bars_since_absorption <= self.initiation_window:
                if self._is_initiation_bar(fast_bar, self.absorption_signal, slow_history):
                    self._place_entry(ctx, self.absorption_signal)
                    self.absorption_signal = None
                    self.bars_since_absorption = 0
            else:
                # Reset if we exceed the initiation window
                self.absorption_signal = None
                self.bars_since_absorption = 0

    def _get_delta_threshold(self) -> float:
        """
        Compute delta threshold using percentile of recent absolute deltas.
        """
        if len(self.fast_delta_history) < self.lookback // 2:
            return 50.0

        abs_deltas = sorted([abs(d) for d in self.fast_delta_history])
        idx = int(len(abs_deltas) * self.delta_percentile)
        idx = max(0, min(idx, len(abs_deltas) - 1))
        threshold: float = float(abs_deltas[idx])

        return max(threshold, 50.0)

    def _get_avg_volume(self) -> float:
        """Get average volume from recent bars."""
        if not self.fast_volume_history:
            return 100.0
        return sum(self.fast_volume_history) / len(self.fast_volume_history)

    def _detect_absorption(self, history) -> Optional[str]:
        """
        Detect absorption: large delta that fails to extend price extremes.
        
        Buy absorption: Large positive delta (buyers aggressive) but fails to
        make a new high relative to prior bar, AND closes below prior close.
        Confirm with prior bar showing positive delta (trend attempt).
        
        Sell absorption: Large negative delta (sellers aggressive) but fails to
        make a new low relative to prior bar, AND closes above prior close.
        Confirm with prior bar showing negative delta (trend attempt).
        """
        if len(history) < 2:
            return None

        last = history[-1]
        prior = history[-2]

        # Volume filter
        avg_vol = self._get_avg_volume()
        if last.volume < avg_vol * self.volume_threshold:
            return None

        threshold = self._get_delta_threshold()

        # Buy absorption: large positive delta but fails to extend high
        if last.delta > threshold:
            # Must fail to make new high AND close below prior close
            if last.high <= prior.high and last.close < prior.close:
                # Confirm with prior bar showing positive delta (trend attempt)
                if prior.delta > 0:
                    return "buy_absorption"

        # Sell absorption: large negative delta but fails to extend low
        if last.delta < -threshold:
            # Must fail to make new low AND close above prior close
            if last.low >= prior.low and last.close > prior.close:
                # Confirm with prior bar showing negative delta (trend attempt)
                if prior.delta < 0:
                    return "sell_absorption"

        return None

    def _check_slow_confirmation(self, absorption_signal: str, slow_history) -> bool:
        """
        Check if the 5m bar trend confirms the intended trade direction.
        
        For buy absorption (want to sell): 5m should be trending down or neutral
        For sell absorption (want to buy): 5m should be trending up or neutral
        """
        if len(slow_history) < 1:
            # If no slow data yet, don't filter
            return True

        slow_bar = slow_history[-1]

        if absorption_signal == "buy_absorption":
            # We want to sell, so 5m should be down or flat
            # Trending down means close < open, or at least not strongly up
            return slow_bar.close <= slow_bar.open or slow_bar.delta <= 0
        elif absorption_signal == "sell_absorption":
            # We want to buy, so 5m should be up or flat
            # Trending up means close > open, or at least not strongly down
            return slow_bar.close >= slow_bar.open or slow_bar.delta >= 0

        return True

    def _is_initiation_bar(self, bar, absorption_signal: str, slow_history) -> bool:
        """
        Check if bar shows initiation: opposite delta flow in the right direction,
        with multi-timeframe confirmation from 5m bar.
        
        For buy absorption (we want to sell): need negative delta
        For sell absorption (we want to buy): need positive delta
        """
        threshold = self._get_delta_threshold()

        # Check 5m confirmation first
        if not self._check_slow_confirmation(absorption_signal, slow_history):
            return False

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta
            if bar.delta < -threshold * self.initiation_delta_strength:
                # Close should be below absorption midpoint
                if bar.close < self.absorption_midpoint:
                    return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta
            if bar.delta > threshold * self.initiation_delta_strength:
                # Close should be above absorption midpoint
                if bar.close > self.absorption_midpoint:
                    return True

        return False

    def _place_entry(self, ctx, absorption_signal: str) -> None:
        """Place entry order based on absorption signal."""
        if absorption_signal == "buy_absorption":
            # Absorption of buying = sell initiation
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-sell",
            )
        elif absorption_signal == "sell_absorption":
            # Absorption of selling = buy initiation
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-buy",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
