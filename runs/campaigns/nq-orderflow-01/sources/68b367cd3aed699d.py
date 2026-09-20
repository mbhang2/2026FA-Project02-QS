"""
Absorption and initiation strategy with refined signal detection and dynamic exits.

Analysis of recent trials:
- Trial #7: -0.0275 (26 trades) - Over-filtered, too strict on confirmation
- Trial #8: -0.1698 (141 trades) - Slight regression from #6
- Trial #6: -0.1967 (158 trades) - Best performance with balanced filtering
- Trial #3: -0.2164 (191 trades) - Good baseline

Key observations:
1. Trial #6 with 158 trades at -0.1967 outperformed trial #8 with 141 trades
   This suggests we're over-filtering and missing good setups
2. The absorption detection itself is solid - prior delta confirmation helps
3. Initiation detection needs refinement - the close positioning threshold matters
4. Commission is significant: need to be selective but not overly restrictive

Hypothesis for improvement:
1. The issue with trial #8 might be the 0.40 close positioning threshold is too loose
   compared to trial #6's 0.33, allowing weaker entries
2. Lookback of 20 bars (trial #8) might be too short for stable percentile calculation
   Revert to 30 bars like trial #6
3. The initiation window of 4 bars (trial #8) might be too long - revert to 3
4. Volume threshold of 0.65 (trial #8) is tighter than 0.60 (trial #6)
   The tighter threshold may be filtering good signals

Refinements for this iteration:
1. Revert to lookback=30 for more stable delta percentile calculation
2. Revert initiation_window to 3 bars - the sweet spot
3. Revert volume_threshold to 0.60 - balanced filtering
4. Tighten close positioning back to 0.35 for initiation (trial #6 was 0.33)
5. Keep single-bar entry (no multi-bar confirmation) - trial #6 worked this way
6. Adjust brackets slightly: 0.38 stop, 0.62 target (middle ground)
7. Keep max_hold at 10 bars - reasonable for commission-aware trading
8. Reduce delta_percentile slightly to 0.70 for slightly more aggressive absorption detection
9. Add a secondary filter: require the absorption bar to have volume > 1.2x average
   This ensures the absorbed volume is truly significant

The key insight: trial #6 was good because it balanced signal strength with
reasonable entry frequency. Trial #7 went too strict, trial #8 went too loose.
We need to return to trial #6's parameters with minor tweaks for optimization.
"""

from collections import deque
from typing import Deque, Optional


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        lookback: int = 30,
        delta_percentile: float = 0.70,
        volume_threshold: float = 0.60,
        volume_multiplier: float = 1.2,
        stop_offset: float = 0.38,
        target_offset: float = 0.62,
        size: int = 1,
        max_hold_bars: int = 10,
        initiation_window: int = 3,
    ) -> None:
        self.fast_interval: str = fast_interval
        self.slow_interval: str = slow_interval
        self.lookback: int = lookback
        self.delta_percentile: float = delta_percentile
        self.volume_threshold: float = volume_threshold
        self.volume_multiplier: float = volume_multiplier
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

        # Volume filters
        avg_vol = self._get_avg_volume()
        if last.volume < avg_vol * self.volume_threshold:
            return None
        if last.volume < avg_vol * self.volume_multiplier:
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
        Detect initiation: opposite delta flow that moves price decisively.
        
        Initiation must:
        1. Have opposite delta from absorption signal (exceed threshold)
        2. Move price decisively in the direction of the delta (close in extreme 35%)
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta + closes in lower portion
            if bar.delta < -threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0.01:
                    # Close in lower 35% of range (price moved down decisively with selling)
                    close_to_low = (bar.high - bar.close) / bar_range
                    if close_to_low > 0.35:
                        return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta + closes in upper portion
            if bar.delta > threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0.01:
                    # Close in upper 35% of range (price moved up decisively with buying)
                    close_to_high = (bar.close - bar.low) / bar_range
                    if close_to_high > 0.35:
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
