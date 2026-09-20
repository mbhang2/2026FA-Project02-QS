"""
Absorption and initiation strategy with enhanced multi-bar confirmation and
adaptive stop placement.

Analysis of trial progression reveals several key insights:

Trial #6: -0.1967 (158 trades) - Best baseline
Trial #20: -0.2083 (149 trades) - Close positioning helped slightly
Trial #21: -0.2026 (157 trades) - Volume confirmation maintained signal

The pattern shows that trials around 150-160 trades with -0.19 to -0.20 scores
perform best. Trials with fewer trades (33, 52, 8) have worse scores despite
lower trade count, suggesting that filtering too aggressively removes good signals.

Key observations from the data:
1. Best performance at 150-160 trades with scores around -0.1967 to -0.2083
2. Trials with <50 trades consistently underperform (scores > -0.10)
3. Trials with >170 trades also underperform (scores < -0.21)
4. The sweet spot appears to be 140-165 trades

This trial aims to improve on #21's -0.2026 by:
1. Using a more nuanced initiation confirmation that doesn't kill signal volume
2. Instead of requiring volume confirmation on initiation, use a softer check:
   the initiation bar should show at least 40% of average volume (vs #21's 50%)
3. Lower initiation_delta_strength to 0.65 (vs #21's 0.70) to catch more reversals
4. Adjust absorption volume_threshold to 0.50 (vs #21's 0.55) to detect more absorptions
5. Add a subtle improvement: check if the initiation bar's delta is MORE extreme
   than the absorption bar's delta (showing stronger conviction in opposite direction)
6. Keep stop_offset at 0.38 and target_offset at 0.65 (proven good)
7. Keep max_hold_bars at 5 (good balance)
8. Keep initiation_window at 2 (tight timing)

Expected outcome: ~155-165 trades with improved score by:
- Lower thresholds increase signal detection without over-filtering
- Delta extremity check adds quality confirmation
- Maintains good trade frequency in the sweet spot
- Better risk/reward from proven parameters
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
        volume_threshold: float = 0.50,
        initiation_volume_threshold: float = 0.40,
        stop_offset: float = 0.38,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 5,
        initiation_window: int = 2,
        initiation_delta_strength: float = 0.65,
    ) -> None:
        self.fast_interval: str = fast_interval
        self.slow_interval: str = slow_interval
        self.lookback: int = lookback
        self.delta_percentile: float = delta_percentile
        self.volume_threshold: float = volume_threshold
        self.initiation_volume_threshold: float = initiation_volume_threshold
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

        # Signal tracking
        self.absorption_signal: Optional[str] = None
        self.bars_since_absorption: int = 0
        self.bars_held: int = 0
        self.absorption_high: float = 0.0
        self.absorption_low: float = 0.0
        self.absorption_midpoint: float = 0.0
        self.absorption_delta: int = 0

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
                self.absorption_delta = fast_history[-1].delta
            return

        # Check for initiation after absorption (within the window)
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            if self.bars_since_absorption <= self.initiation_window:
                if self._is_initiation_bar(fast_bar, self.absorption_signal):
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

    def _is_initiation_bar(self, bar, absorption_signal: str) -> bool:
        """
        Check if bar shows strong initiation with close positioning and volume filters.
        
        For buy absorption (we want to sell): need strong negative delta AND
        close below the absorption bar's midpoint AND decent volume AND
        the delta must be more extreme (more negative) than absorption bar.
        
        For sell absorption (we want to buy): need strong positive delta AND
        close above the absorption bar's midpoint AND decent volume AND
        the delta must be more extreme (more positive) than absorption bar.
        """
        threshold = self._get_delta_threshold()
        avg_vol = self._get_avg_volume()

        # Volume confirmation on initiation bar - softer threshold
        if bar.volume < avg_vol * self.initiation_volume_threshold:
            return False

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta
            if bar.delta < -threshold * self.initiation_delta_strength:
                # Close should be below absorption midpoint
                if bar.close < self.absorption_midpoint:
                    # Delta should be more extreme (more negative) than absorption
                    if bar.delta < self.absorption_delta:
                        return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta
            if bar.delta > threshold * self.initiation_delta_strength:
                # Close should be above absorption midpoint
                if bar.close > self.absorption_midpoint:
                    # Delta should be more extreme (more positive) than absorption
                    if bar.delta > self.absorption_delta:
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
