"""
Absorption and initiation strategy with adaptive entry timing and volume confirmation.

Analysis of recent trials reveals that the best performers balance:
- Trial #6: -0.1967 (158 trades) - Baseline with good signal frequency
- Trial #20: -0.2083 (149 trades) - Close positioning filter helped slightly
- Trial #19: -0.1534 (52 trades) - Too restrictive, killed signal

Key observations:
1. Trial #20 improved over #6 by using midpoint-based close positioning
2. But the initiation_delta_strength of 0.80 may be too high
3. Trial #19 showed that multi-bar confirmation reduces trades but doesn't improve score

New strategy for this trial:
1. Keep absorption detection from #6/#20 (proven effective)
2. Lower initiation_delta_strength to 0.70 (less restrictive than #20's 0.80)
3. Use midpoint-based close positioning like #20 (softer than prior close)
4. Add volume confirmation on initiation bar: must have decent volume
5. Fine-tune parameters:
   - stop_offset: 0.38 (slightly tighter than #20's 0.39)
   - target_offset: 0.65 (proven good)
   - max_hold_bars: 5 (good balance)
   - initiation_delta_strength: 0.70 (less restrictive)
   - volume_threshold: 0.55 (slightly lower for absorption detection)
6. Add initiation volume confirmation: initiation bar volume >= 50% of average

Expected outcome: ~160-180 trades with improved score by:
- Lower delta strength threshold increases signal detection
- Volume confirmation on initiation adds robustness
- Close positioning filter removes false signals
- Tighter stops reduce losses
- Balanced parameters maintain good trade frequency
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
        volume_threshold: float = 0.55,
        initiation_volume_threshold: float = 0.50,
        stop_offset: float = 0.38,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 5,
        initiation_window: int = 2,
        initiation_delta_strength: float = 0.70,
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
        close below the absorption bar's midpoint AND decent volume.
        For sell absorption (we want to buy): need strong positive delta AND
        close above the absorption bar's midpoint AND decent volume.
        """
        threshold = self._get_delta_threshold()
        avg_vol = self._get_avg_volume()

        # Volume confirmation on initiation bar
        if bar.volume < avg_vol * self.initiation_volume_threshold:
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
