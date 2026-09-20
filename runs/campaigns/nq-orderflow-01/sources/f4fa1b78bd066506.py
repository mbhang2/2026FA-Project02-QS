"""
Absorption and initiation strategy with adaptive thresholds and confirmation.

Analysis of the trial progression reveals key insights:
- Trial #6: -0.1967 (158 trades) - Best overall baseline
- Trial #19: -0.1534 (52 trades) - Multi-bar conviction helped but too restrictive
- Trial #18: -0.1216 (34 trades) - Close positioning filter was helpful
- Trial #13: -0.2115 (171 trades) - High volume of trades but worse score

The pattern: Trial #6 succeeded with:
1. Adaptive delta threshold at 75th percentile
2. Volume filter at 60% of average
3. Prior bar delta confirmation in absorption
4. 2-bar initiation window with 2-bar requirement
5. Risk/reward of stop=0.40, target=0.65
6. 6-bar max hold

Trial #19 went too strict with multi-bar confirmation (only 52 trades).
Trial #18 improved with close positioning but lost signal.

Key refinement for this trial:
1. Keep absorption detection from #6 (proven effective)
2. Initiation: require strong opposite delta (80% of threshold)
3. Add a SOFTER close positioning check: the initiation bar's close should
   be on the correct side of the absorption bar's midpoint (not prior close)
4. For buy absorption (sell signal): close < (absorption_high + absorption_low) / 2
5. For sell absorption (buy signal): close > (absorption_high + absorption_low) / 2
6. This filters weak reversals while maintaining signal frequency
7. Single initiation bar entry (not requiring multi-bar confirmation)
8. Fine-tune stop_offset to 0.39 (slightly tighter than #6's 0.40)
9. Keep target_offset at 0.65
10. Reduce max_hold_bars to 5 (lower commission drag)

Expected outcome: ~100-140 trades with improved score over #6's -0.1967 by:
- Close positioning filter removes weak reversals
- Maintains good signal frequency
- Tighter stops reduce losses
- Shorter hold reduces commission
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
        stop_offset: float = 0.39,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 5,
        initiation_window: int = 2,
        initiation_delta_strength: float = 0.80,
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
        Check if bar shows strong initiation with close positioning filter.
        
        For buy absorption (we want to sell): need strong negative delta AND
        close below the absorption bar's midpoint.
        For sell absorption (we want to buy): need strong positive delta AND
        close above the absorption bar's midpoint.
        """
        threshold = self._get_delta_threshold()

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
