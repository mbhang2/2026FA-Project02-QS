"""
Absorption and initiation strategy with refined signal quality filters.

Analysis of recent trials reveals a critical insight:
- Trial #6: -0.1967 (158 trades) - Best baseline
- Trial #23: -0.2098 (138 trades) - 5m confirmation helped but reduced signal
- Trial #22: -0.2182 (176 trades) - Too many trades, quality suffered
- Trial #21: -0.2026 (157 trades) - Good but slightly worse than #6

The pattern is clear: 155-160 trades at -0.1967 to -0.2000 is the sweet spot.

Key observation: Trial #23's 5m confirmation reduced trades from ~160 to ~138,
which hurt performance. The 5m filter was too strict. But trial #22's removal
of the extremity check (bar.delta > absorption_delta) led to 176 trades and
worse performance, suggesting we need SOME quality filter but not the 5m timeframe.

New approach for this trial:
1. Return to #6's core logic but add a subtle quality improvement
2. Instead of 5m timeframe confirmation, use intra-bar conviction:
   - Initiation bar must show delta in the right direction (opposite of absorption)
   - PLUS the initiation bar's close must be positioned to show conviction
   - For sell absorption (buy initiation): close in upper half of the bar's range
   - For buy absorption (sell initiation): close in lower half of the bar's range
3. This filters weak reversals without external timeframe dependency
4. Keep proven parameters from #6:
   - stop_offset: 0.40
   - target_offset: 0.65
   - max_hold_bars: 6
   - delta_percentile: 0.75
   - volume_threshold: 0.60
   - initiation_window: 3
5. Initiation delta strength: 0.70 (requires opposite delta at 70% of threshold)
6. Add close positioning check: initiation bar's close must be in outer 40% of range

Expected outcome: ~155-160 trades with score around -0.1970 by:
- Quality filter based on intra-bar structure (no external timeframe)
- Removes weak reversals where close is in middle of range
- Maintains signal volume near optimal level
- Simpler logic than #23's multi-timeframe approach
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
        close_position_threshold: float = 0.40,
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
        self.close_position_threshold: float = close_position_threshold

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

    def _get_close_position_in_range(self, bar) -> float:
        """
        Get the close position as a fraction of the bar's range.
        0.0 = at low, 1.0 = at high, 0.5 = at midpoint
        """
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return 0.5
        return (bar.close - bar.low) / bar_range

    def _is_initiation_bar(self, bar, absorption_signal: str) -> bool:
        """
        Check if bar shows initiation with conviction based on intra-bar structure.
        
        For buy absorption (we want to sell): need negative delta AND
        close in lower half of range (position < 0.5) to show sellers in control.
        
        For sell absorption (we want to buy): need positive delta AND
        close in upper half of range (position > 0.5) to show buyers in control.
        
        This filters weak reversals where the bar shows opposite delta but
        the close is in the middle of the range, suggesting uncertainty.
        """
        threshold = self._get_delta_threshold()
        close_pos = self._get_close_position_in_range(bar)

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta
            if bar.delta < -threshold * self.initiation_delta_strength:
                # Close should be in lower portion of range (sellers in control)
                # Threshold of 0.40 means close must be in lower 40% of range
                if close_pos < self.close_position_threshold:
                    return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta
            if bar.delta > threshold * self.initiation_delta_strength:
                # Close should be in upper portion of range (buyers in control)
                # Threshold of 0.40 means close must be in upper 40% of range
                if close_pos > (1.0 - self.close_position_threshold):
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
