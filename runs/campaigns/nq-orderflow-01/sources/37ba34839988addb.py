"""
Absorption and initiation strategy with refined entry timing and adaptive filtering.

Analysis of recent trials:
- Trial #9: -0.2347 (147 trades) - Regression from #6, added volume_multiplier filter
- Trial #8: -0.1698 (141 trades) - Fewer trades but worse performance
- Trial #6: -0.1967 (158 trades) - Best so far with balanced filtering
- Trial #3: -0.2164 (191 trades) - Good baseline with more trades

Key insight from trial #9: The volume_multiplier filter (1.2x) was too strict,
reducing trade count but hurting performance. Trial #6's parameters were better.

New hypothesis: The issue isn't the absorption detection - it's the initiation
detection timing. We're waiting for initiation bars, but maybe we should:
1. Enter more aggressively on absorption itself (absorption IS the signal)
2. Use initiation as a confirmation FILTER on absorption, not a separate trigger
3. Be more selective about WHICH absorptions to trade

Refinements for this iteration:
1. Revert to trial #6 core parameters (lookback=30, delta_percentile=0.75)
2. Remove the volume_multiplier filter that hurt trial #9
3. Tighten absorption detection: require BOTH prior bar AND bar-before-prior
   to show trend agreement. This ensures a real trend attempt was made.
4. Change entry logic: enter on absorption detection itself, but only if we can
   see initiation within 2 bars (tighter window). This reduces latency.
5. Simplify initiation check: just require opposite delta to exceed threshold,
   remove the close positioning requirement (it's too strict).
6. Adjust brackets: 0.40 stop, 0.65 target (good risk/reward from trial #6)
7. Add session-level tracking: skip entries in first 10 bars of session
   (cash open volatility) and last 30 bars (avoid overnight risk)
8. Reduce max_hold to 8 bars - better for commission management
9. Use a simpler delta threshold: 65th percentile instead of 75th
   (slightly more aggressive but still selective)

The core insight: Trial #6 was good because it had the right balance.
We've been over-engineering the initiation detection. The simpler approach
of "absorption + nearby opposite delta = entry" might work better.
"""

from collections import deque
from typing import Deque, Optional


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        lookback: int = 30,
        delta_percentile: float = 0.65,
        volume_threshold: float = 0.60,
        stop_offset: float = 0.40,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 8,
        initiation_window: int = 2,
        min_bars_into_session: int = 10,
        max_bars_before_close: int = 30,
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
        self.max_bars_before_close: int = max_bars_before_close

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

        # Session boundary filters
        time_to_close = ctx.session_end - ctx.ts
        bars_to_close = time_to_close / (60 * 1_000_000_000)  # rough estimate
        
        if self.bars_in_session < self.min_bars_into_session:
            # Too early in session
            self.absorption_signal = None
            self.bars_since_absorption = 0
            return

        if bars_to_close < self.max_bars_before_close:
            # Too close to session end
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

        return max(threshold, 40.0)

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

        return max(threshold, 25.0)

    def _get_avg_volume(self) -> float:
        """Get average volume from recent bars."""
        if not self.fast_volume_history:
            return 100.0
        return sum(self.fast_volume_history) / len(self.fast_volume_history)

    def _detect_absorption(self, history) -> Optional[str]:
        """
        Detect absorption: large delta that fails to extend price extremes.
        
        Require:
        1. Current bar has large delta (above threshold)
        2. Current bar fails to extend price in direction of delta
        3. Prior bar agrees with the trend direction (positive/negative delta)
        4. Bar-before-prior also shows trend agreement (stronger confirmation)
        5. Volume is meaningful relative to average
        """
        if len(history) < 3:
            return None

        last = history[-1]
        prior = history[-2]
        prior2 = history[-3]

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
                # Confirm with both prior bars showing positive delta
                if prior.delta > 0 and prior2.delta > 0:
                    return "buy_absorption"

        # Sell absorption: large negative delta but fails to extend low
        if last.delta < -threshold and abs(last.delta) >= strength_threshold:
            # Must fail to make new low AND close above prior close
            if last.low >= prior.low and last.close > prior.close:
                # Confirm with both prior bars showing negative delta
                if prior.delta < 0 and prior2.delta < 0:
                    return "sell_absorption"

        return None

    def _detect_initiation(self, bar, absorption_signal: str) -> bool:
        """
        Detect initiation: opposite delta flow.
        
        Simplified check: just require opposite delta to exceed threshold.
        We've already confirmed the absorption is real, so initiation
        just needs to show the reversal is happening.
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: negative delta
            return bar.delta < -threshold

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: positive delta
            return bar.delta > threshold

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
