"""
Absorption and initiation strategy with refined entry timing and exit logic.

Analysis of trial #7 (score -0.0275, 26 trades):
- Drastically reduced trades but worse performance suggests over-filtering
- The multi-bar confirmation (2 bars required) is too strict
- Loss of signal quality despite fewer entries indicates we're missing good setups
- The 0.33 close positioning threshold is likely filtering winners

Analysis of trial #6 (score -0.1967, 158 trades):
- Better performance with more reasonable trade count
- The prior delta confirmation works well
- 3-bar initiation window is appropriate
- Brackets at 0.35/0.60 are reasonable

Key insight: Trial #7 over-corrected. The multi-bar confirmation requirement
means we often miss the actual reversal bar - we're waiting for confirmation
when the first initiation bar IS the reversal.

Refinements for this iteration:

1. Remove multi-bar confirmation requirement - go back to single-bar entry
   when initiation is detected. The first bar showing opposite delta flow
   that moves price is likely the actual reversal point.

2. Loosen close positioning threshold: 0.40 instead of 0.33. We don't need
   extreme positioning, just evidence the delta moved price.

3. Keep the strength threshold (50th percentile) for absorption detection -
   this filters weak signals effectively.

4. Reduce lookback to 20 bars - faster adaptation to changing delta regime.

5. Increase volume threshold to 0.65 - we want absorbed volume to be meaningful.

6. Extend initiation window to 4 bars - give reversal more room to develop.

7. Adjust brackets: 0.40 stop, 0.65 target (was 0.35/0.60) - better risk/reward.

8. Increase max_hold to 12 bars - let winners run longer since we're more selective.

9. Simplify: the absorption signal itself is the entry trigger, initiation just
   confirms the direction. Don't wait for initiation confirmation - use it as
   a filter on the absorption detection itself. If we see absorption AND can
   find initiation within the window, we enter. But don't require multi-bar.

10. Alternative approach: what if the issue is that we're looking for initiation
    in the WRONG bars? We detect absorption on bar N. Initiation might appear
    on bar N+1 or N+2, but we're requiring it to be "strong". What if we just
    need to see ANY opposite delta in the window, and that's our signal to enter?
"""

from collections import deque
from typing import Deque, Optional


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        lookback: int = 20,
        delta_percentile: float = 0.75,
        volume_threshold: float = 0.65,
        stop_offset: float = 0.40,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 12,
        initiation_window: int = 4,
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
            return

        # Check for initiation after absorption (within the window)
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            if self.bars_since_absorption <= self.initiation_window:
                if self._detect_initiation(fast_bar, self.absorption_signal):
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

    def _get_delta_strength_threshold(self) -> float:
        """
        Compute 50th percentile of absolute deltas for signal strength check.
        """
        if len(self.fast_delta_history) < self.lookback // 2:
            return 30.0

        abs_deltas = sorted([abs(d) for d in self.fast_delta_history])
        idx = int(len(abs_deltas) * 0.50)
        idx = max(0, min(idx, len(abs_deltas) - 1))
        threshold: float = float(abs_deltas[idx])

        return max(threshold, 30.0)

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
        Also require absorption delta to be in top 50% of strength.
        
        Sell absorption: Large negative delta (sellers aggressive) but fails to
        make a new low relative to prior bar, AND closes above prior close.
        Confirm with prior bar showing negative delta (trend attempt).
        Also require absorption delta to be in top 50% of strength.
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
        strength_threshold = self._get_delta_strength_threshold()

        # Buy absorption: large positive delta but fails to extend high
        if last.delta > threshold and abs(last.delta) >= strength_threshold:
            # Must fail to make new high AND close below prior close
            if last.high <= prior.high and last.close < prior.close:
                # Confirm with prior bar showing positive delta (trend attempt)
                if prior.delta > 0:
                    return "buy_absorption"

        # Sell absorption: large negative delta but fails to extend low
        if last.delta < -threshold and abs(last.delta) >= strength_threshold:
            # Must fail to make new low AND close above prior close
            if last.low >= prior.low and last.close > prior.close:
                # Confirm with prior bar showing negative delta (trend attempt)
                if prior.delta < 0:
                    return "sell_absorption"

        return None

    def _detect_initiation(self, bar, absorption_signal: str) -> bool:
        """
        Detect initiation: opposite delta flow that moves price.
        
        Initiation must:
        1. Have opposite delta from absorption signal (exceed threshold)
        2. Move price in the direction of the delta (close positioning at 40%)
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta + closes in lower portion
            if bar.delta < -threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0.01:
                    # Close in lower 40% of range
                    close_to_low = (bar.high - bar.close) / bar_range
                    if close_to_low > 0.40:
                        return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta + closes in upper portion
            if bar.delta > threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0.01:
                    # Close in upper 40% of range
                    close_to_high = (bar.close - bar.low) / bar_range
                    if close_to_high > 0.40:
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
