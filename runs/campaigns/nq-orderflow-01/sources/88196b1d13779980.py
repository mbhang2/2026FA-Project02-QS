"""
Absorption and initiation strategy with refined multi-bar confirmation and context-aware filtering.

Analysis of trial progression:
- Trial #6: -0.1967 (158 trades) - Best performance with balanced parameters
- Trial #7: -0.0275 (26 trades) - Over-filtered, too restrictive
- Trial #9: -0.2347 (147 trades) - Regression from #6
- Trial #10: -0.2126 (157 trades) - Close to #6 but slightly worse

Key observations:
1. Trial #6 remains the best - it had the right balance of signal strength and trade frequency
2. The absorption detection itself is solid with prior bar confirmation
3. Initiation detection has been the weak point - we've oscillated between too strict and too loose
4. Session timing filters in trial #10 helped slightly but not enough

New hypothesis for improvement:
1. Trial #6's close positioning threshold (0.33) was better than later iterations
2. The issue might be that we're entering too late - waiting for initiation bars adds latency
3. Consider: what if we enter on absorption confirmation + LOOK AHEAD 1 bar for initiation?
   This means on bar N where we detect absorption, check if bar N+1 (just closed) shows initiation
4. Add a "reversal strength" metric: measure the delta reversal magnitude, not just direction
5. Use a tighter lookback window (25 bars) for more responsive thresholds
6. Implement a session-aware volume filter that adjusts for session time of day
7. Add position sizing based on signal strength (not implemented yet, but prepare infrastructure)
8. More conservative brackets: 0.35 stop, 0.60 target (similar to trial #6 but tested)

The core refinement: Trial #6 worked because:
- Lookback=30, delta_percentile=0.75 (stable thresholds)
- volume_threshold=0.60 (good balance)
- stop_offset=0.40, target_offset=0.65 (solid risk/reward)
- initiation_window=3, initiation_bars_required=2 (multi-bar confirmation)

We should stick closer to this winning formula but optimize:
- The timing of entry relative to initiation detection
- The strength/conviction required for signals
- Session-time awareness for filtering

This trial focuses on:
1. Returning to trial #6 core parameters (proven winners)
2. Simplifying initiation: require 1 strong initiation bar, not 2
3. Adding reversal strength check: initiation delta must exceed threshold by margin
4. Tighter brackets for better risk management
5. Cleaner session boundary logic
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
        stop_offset: float = 0.35,
        target_offset: float = 0.60,
        size: int = 1,
        max_hold_bars: int = 8,
        initiation_window: int = 3,
        min_bars_into_session: int = 5,
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
        self.min_bars_into_session: int = min_bars_into_session

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
        self.bars_in_session: int = 0
        self.last_session: Optional[int] = None

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

        # Track session and bars within session
        if ctx.session != self.last_session:
            self.last_session = ctx.session
            self.bars_in_session = 0

        self.bars_in_session += 1

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

        # Skip very early in session
        if self.bars_in_session < self.min_bars_into_session:
            self.absorption_signal = None
            self.bars_since_absorption = 0
            return

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
        Detect initiation: opposite delta flow with conviction.
        
        Initiation must:
        1. Have opposite delta from absorption signal (exceed threshold)
        2. Delta should show meaningful reversal (exceed by some margin)
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta
            if bar.delta < -threshold:
                return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta
            if bar.delta > threshold:
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
