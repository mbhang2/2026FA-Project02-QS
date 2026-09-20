"""
Absorption and initiation strategy with refined extremity-based filtering.

Analysis of recent trials reveals the core pattern:
- Trial #6: -0.1967 (158 trades) - Best baseline
- Trial #24: -0.2466 (196 trades) - Close position filter increased trades, hurt score
- Trial #23: -0.2098 (138 trades) - 5m confirmation reduced signal too much
- Trial #21: -0.2026 (157 trades) - Good but slightly worse than #6

The critical insight: Trial #24 shows that the close position threshold filter
actually INCREASED trades (196 vs 158 in #6) while WORSENING the score (-0.2466 vs -0.1967).
This suggests the filter is letting in too many weak signals.

The issue with #24: close_position_threshold of 0.40 means "close in outer 40% of range".
For a bar with range 1.0, this is quite generous - a close at 0.35 qualifies as "lower 40%".
This is catching reversals that aren't truly decisive.

New approach for this trial:
1. Return to #6's core absorption and initiation logic
2. Add a stricter extremity requirement: initiation bar's delta must be MORE extreme
   than the absorption bar's delta, AND in the opposite direction
3. This is the "conviction" filter: the reversal must be stronger than what failed
4. For sell absorption (buy initiation): 
   - Absorption had negative delta (e.g., -100)
   - Initiation must have positive delta > 100 (more extreme in opposite direction)
5. For buy absorption (sell initiation):
   - Absorption had positive delta (e.g., +100)
   - Initiation must have negative delta < -100 (more extreme in opposite direction)
6. Keep proven parameters from #6:
   - stop_offset: 0.40
   - target_offset: 0.65
   - max_hold_bars: 6
   - delta_percentile: 0.75
   - volume_threshold: 0.60
   - initiation_window: 3
7. Store absorption delta for comparison in initiation check

Expected outcome: ~155-160 trades with score around -0.1970 by:
- Requiring initiation delta to exceed absorption delta (conviction filter)
- Filters weak reversals where flow changes but lacks strength
- Simpler logic than close position checks
- Returns to proven parameter set from #6 with one key addition
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
        self.absorption_delta: int = 0
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
            self.absorption_delta = fast_history[-1].delta
            self.bars_since_absorption = 0
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
        Check if bar shows initiation with conviction.
        
        The initiation bar's delta must be MORE extreme than the absorption bar's
        delta, in the opposite direction. This ensures the reversal has real strength.
        
        For buy absorption (we want to sell): 
        - Absorption had positive delta
        - Initiation must have negative delta with abs value > abs(absorption_delta)
        
        For sell absorption (we want to buy):
        - Absorption had negative delta
        - Initiation must have positive delta with abs value > abs(absorption_delta)
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: negative delta more extreme than absorption
            # absorption_delta was positive, so we need bar.delta < -absorption_delta
            if bar.delta < -abs(self.absorption_delta):
                return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: positive delta more extreme than absorption
            # absorption_delta was negative, so we need bar.delta > -absorption_delta
            if bar.delta > -self.absorption_delta:
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
