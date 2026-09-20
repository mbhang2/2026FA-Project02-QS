"""
Absorption and initiation strategy with adaptive entry timing and refined filtering.

Analysis of trial progression:
- Trial #6: -0.1967 (158 trades) - Best overall performance
- Trial #14: -0.0245 (27 trades) - Over-filtered, lost signal
- Trial #13: -0.2115 (171 trades) - Good but slight regression
- Trial #11: -0.2212 (167 trades) - Close to #6

The pattern shows trial #6 was optimal. Trials #14 and #12 added too many filters
(close positioning threshold, multi-bar confirmation) and drastically reduced trades
while degrading scores.

Key insight from #6's success:
1. Lookback=30, percentile=0.75 - stable adaptive thresholds
2. volume_threshold=0.60 - good balance
3. stop_offset=0.40, target_offset=0.65 - solid risk/reward
4. initiation_window=3, initiation_bars_required=2 - reasonable confirmation
5. Prior bar delta confirmation filters noise effectively
6. NO position-based filters that over-constrain

The problem with recent trials:
- Trial #14 added close_positioning_threshold=0.25 → 27 trades (catastrophic)
- Trial #13 removed it but kept other complexity → 171 trades (slight regression)
- The positioning threshold was the killer - it filtered out too many valid signals

New hypothesis for this trial:
Return to trial #6's core winning formula but with micro-refinements:
1. Keep absorption detection exactly as in #6 (prior bar delta confirmation works)
2. Simplify initiation: require strong opposite delta + prior bar confirmation
3. Add prior bar confirmation to initiation (not position-based, just delta agreement)
4. This maintains signal volume while filtering weak reversals
5. Reduce max_hold to 5 bars (more aggressive exit, less commission drag)
6. Slightly tighter stop (0.38) for better risk management

The refinement: Trial #6 + prior bar delta confirmation in initiation detection
instead of position-based filters. This should:
- Maintain ~150-160 trade volume
- Keep the proven confirmation logic that works
- Reduce commission drag with shorter holds
- Improve risk/reward with tighter stops
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
        stop_offset: float = 0.38,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 5,
        initiation_window: int = 3,
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
            return

        # Check for initiation after absorption (within the window)
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            if self.bars_since_absorption <= self.initiation_window:
                if self._is_initiation_bar(fast_bar, fast_history, self.absorption_signal):
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

    def _is_initiation_bar(self, bar, history, absorption_signal: str) -> bool:
        """
        Check if bar shows initiation: opposite delta flow with prior bar confirmation.
        
        Initiation requires:
        1. Strong opposite delta (exceeds threshold)
        2. Prior bar shows agreement with initiation direction (delta sign matches)
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta
            if bar.delta < -threshold:
                # Check prior bar agreement: it should show negative delta
                if len(history) >= 1:
                    prior = history[-1]
                    if prior.delta < 0:
                        return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta
            if bar.delta > threshold:
                # Check prior bar agreement: it should show positive delta
                if len(history) >= 1:
                    prior = history[-1]
                    if prior.delta > 0:
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
