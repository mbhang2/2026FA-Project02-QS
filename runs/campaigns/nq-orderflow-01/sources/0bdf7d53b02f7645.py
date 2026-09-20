"""
Absorption and initiation strategy with adaptive entry timing and confirmation.

Analysis of recent trials reveals a critical pattern:
- Trial #6: -0.1967 (158 trades) - Best overall baseline
- Trial #17: -0.0838 (23 trades) - Close positioning filter too restrictive
- Trial #16: -0.0125 (8 trades) - 3-bar requirement killed signal
- Trial #15: -0.0987 (33 trades) - Prior bar delta confirmation too strict

The issue with #17: requiring close in the right half of the bar combined with
strong delta was too constraining (only 23 trades). The close positioning filter
is valid but needs to be less strict.

Key insight from #6's success:
1. Adaptive delta threshold at 75th percentile works well
2. Volume filter at 60% of average is appropriate
3. Prior bar delta confirmation in absorption is effective
4. Simple 2-bar initiation window maintains signal
5. Risk/reward of stop=0.40, target=0.65 is solid

Refinement strategy for this trial:
1. Keep absorption detection from #6 exactly (prior bar delta confirmation)
2. Modify initiation filter: require strong opposite delta but use a SOFTER
   close positioning check - only require close to be on the "correct side"
   (not necessarily in the extreme half)
3. For buy absorption (sell signal): close < prior bar close (not bar midpoint)
4. For sell absorption (buy signal): close > prior bar close (not bar midpoint)
5. This is much less restrictive than #17 but still filters weak reversals
6. Keep 2-bar initiation requirement (good balance)
7. Increase max_hold_bars back to 6 (commission drag is less important than signal)
8. Adjust stop_offset to 0.42 (between #6's 0.40 and #17's 0.38)
9. Keep target_offset at 0.65 (proven effective)

Expected outcome: ~100-150 trades with improved score over #6's -0.1967 by:
- Filtering weak reversals (close positioning relative to prior bar)
- Maintaining signal frequency (softer filter than #17)
- Better risk management (stop at 0.42)
- Reasonable hold period (6 bars)
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
        stop_offset: float = 0.42,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 6,
        initiation_window: int = 2,
        initiation_bars_required: int = 2,
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
        self.initiation_bars_required: int = initiation_bars_required

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
        self.initiation_bar_count: int = 0
        self.absorption_prior_close: float = 0.0

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
            self.initiation_bar_count = 0
            return

        # Reset when flat
        self.bars_held = 0

        # Detect absorption on fast bars
        absorption = self._detect_absorption(fast_history)

        if absorption:
            self.absorption_signal = absorption
            self.bars_since_absorption = 0
            self.initiation_bar_count = 0
            # Store the prior bar's close for later initiation filtering
            if len(fast_history) >= 2:
                self.absorption_prior_close = fast_history[-2].close
            return

        # Check for initiation after absorption (within the window)
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            if self.bars_since_absorption <= self.initiation_window:
                if self._is_initiation_bar(fast_bar, self.absorption_signal):
                    self.initiation_bar_count += 1
                    # Only enter after required number of consecutive initiation bars
                    if self.initiation_bar_count >= self.initiation_bars_required:
                        self._place_entry(ctx, self.absorption_signal)
                        self.absorption_signal = None
                        self.bars_since_absorption = 0
                        self.initiation_bar_count = 0
                else:
                    # Reset counter if bar doesn't show initiation
                    self.initiation_bar_count = 0
            else:
                # Reset if we exceed the initiation window
                self.absorption_signal = None
                self.bars_since_absorption = 0
                self.initiation_bar_count = 0

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
        Check if bar shows initiation: opposite delta flow with softer close filter.
        
        For buy absorption (we want to sell): need negative delta AND close below
        the absorption bar's prior close (softer than bar midpoint).
        For sell absorption (we want to buy): need positive delta AND close above
        the absorption bar's prior close.
        
        This filters weak reversals while maintaining signal frequency.
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta
            if bar.delta < -threshold * 0.65:
                # Close should be below the absorption bar's prior close
                if bar.close < self.absorption_prior_close:
                    return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta
            if bar.delta > threshold * 0.65:
                # Close should be above the absorption bar's prior close
                if bar.close > self.absorption_prior_close:
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
