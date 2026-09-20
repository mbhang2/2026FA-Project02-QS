"""
Absorption and initiation strategy with refined entry timing and reversal conviction.

Analysis of trial #11 (best recent: -0.2212, 167 trades):
- Very close to trial #6 (-0.1967, 158 trades) which remains best overall
- Trial #7 (-0.0275, 26 trades) was too restrictive
- The core absorption + initiation logic is sound

Key insight from the progression:
Trial #6 is still the best performer at -0.1967 with 158 trades.
Trial #11 regressed slightly to -0.2212 with 167 trades (more trades, worse results).

The pattern suggests:
1. The absorption detection with prior bar confirmation is good
2. The initiation detection needs to be MORE selective, not less
3. We may be entering on weak initiation signals
4. Trial #6's parameters (lookback=30, percentile=0.75, 2 initiation bars required)
   were actually optimal

New hypothesis for this trial:
1. Return to trial #6's core winning parameters
2. Add REVERSAL MAGNITUDE check: initiation delta must exceed absorption delta
   in absolute value. This ensures a real reversal, not just opposite direction.
3. Tighten the initiation window to 2 bars (faster entry, less latency)
4. Require initiation bar to close with conviction (in outer 40% of range)
5. Add a "delta consistency" check: the bar before absorption must also show
   trend agreement (not just the immediate prior)
6. Use 75th percentile for threshold (trial #6 was right)
7. Brackets: 0.40 stop, 0.65 target (trial #6 values, proven)
8. Max hold: 8 bars (good balance between commission and trend capture)
9. Remove session timing filters - they may be too restrictive
10. Focus on QUALITY over QUANTITY: fewer trades that work better

The core refinement from trial #6:
- Add reversal magnitude requirement: initiation delta must be stronger than absorption
- Add close positioning for initiation (conviction in direction)
- Tighten initiation window to 2 bars
- This should filter out weak reversals while keeping strong ones
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
        max_hold_bars: int = 8,
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
        self.absorption_delta: int = 0
        self.bars_since_absorption: int = 0
        self.bars_held: int = 0
        self.initiation_bar_count: int = 0

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
            signal, delta = absorption
            self.absorption_signal = signal
            self.absorption_delta = delta
            self.bars_since_absorption = 0
            self.initiation_bar_count = 0
            return

        # Check for initiation after absorption (within the window)
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            if self.bars_since_absorption <= self.initiation_window:
                if self._is_initiation_bar(fast_bar, self.absorption_signal, self.absorption_delta):
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

    def _detect_absorption(self, history) -> Optional[tuple[str, int]]:
        """
        Detect absorption: large delta that fails to extend price extremes.
        
        Buy absorption: Large positive delta (buyers aggressive) but fails to
        make a new high relative to prior bar, AND closes below prior close.
        Confirm with prior bar showing positive delta (trend attempt).
        Also require absorption delta to be in top 50% of strength.
        
        Returns: (signal_type, absorption_delta) or None
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
                    return ("buy_absorption", last.delta)

        # Sell absorption: large negative delta but fails to extend low
        if last.delta < -threshold and abs(last.delta) >= strength_threshold:
            # Must fail to make new low AND close above prior close
            if last.low >= prior.low and last.close > prior.close:
                # Confirm with prior bar showing negative delta (trend attempt)
                if prior.delta < 0:
                    return ("sell_absorption", last.delta)

        return None

    def _is_initiation_bar(self, bar, absorption_signal: str, absorption_delta: int) -> bool:
        """
        Check if bar shows initiation: opposite delta flow with conviction.
        
        Initiation must:
        1. Have opposite delta from absorption signal (exceed threshold)
        2. Reversal magnitude: initiation delta must exceed absorption delta in strength
        3. Close with conviction in the direction of delta (in outer 40% of range)
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta
            if bar.delta < -threshold:
                # Check reversal magnitude: initiation must be stronger than absorption
                if abs(bar.delta) > abs(absorption_delta):
                    # Check close positioning: close in lower 40% of range
                    bar_range = bar.high - bar.low
                    if bar_range > 0.01:
                        close_to_low = (bar.high - bar.close) / bar_range
                        if close_to_low > 0.40:
                            return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta
            if bar.delta > threshold:
                # Check reversal magnitude: initiation must be stronger than absorption
                if abs(bar.delta) > abs(absorption_delta):
                    # Check close positioning: close in upper 40% of range
                    bar_range = bar.high - bar.low
                    if bar_range > 0.01:
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
