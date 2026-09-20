"""
Absorption and initiation strategy with refined signal detection and cost management.

Prior trials scored -0.6165, -0.3989, and -0.3583. The progression suggests:
1. The multi-timeframe filtering (trial #2) helped but may be too restrictive
2. The 5-bar max hold with early exit on no profit is too aggressive
3. Commission costs (0.1 index points = $2/contract per side) are significant

Key insights from prior trials:
- Absorption detection works (percentile-based thresholds are sound)
- Initiation detection is sound (opposite delta with price agreement)
- Multi-timeframe filtering reduced noise but may have filtered too much
- Position management was too eager to exit

Refinements for this iteration:

1. Loosen multi-timeframe filter: Instead of requiring 5m trend alignment,
   use 5m as a regime filter only - allow trades in both directions but
   require the 1m signal to be clean. This captures more valid signals.

2. Extend hold period: 5 bars is too short. Commission on round-trip is ~$4,
   so we need bigger moves. Extend to 8-10 bars and only exit on loss if
   we've held 10+ bars (not 5).

3. Improve absorption detection: Current logic requires close below/above
   prior close. Strengthen by requiring the bar to fail to extend the prior
   bar's extremes (high/low), not just close direction. This is more aligned
   with the "absorption" concept.

4. Tighter initiation window: Initiation must come within 2 bars (not just
   the very next bar). This gives more flexibility while staying tight.

5. Reduce position size only when necessary: Keep size=1 but optimize
   stop/target offsets. Use 0.40 stop, 0.65 target (wider target to catch
   bigger moves and overcome commission).

6. Remove early-session multiplier: The data may not have strong early-session
   effects. Simplify to single threshold approach.

7. Volume filter: Keep it but use 0.60 threshold (slightly less restrictive).
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
        volume_threshold: float = 0.60,
        stop_offset: float = 0.40,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 10,
        initiation_window: int = 2,
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

        # Slow (5m) for regime context only
        self.slow_close_history: Deque[float] = deque(maxlen=5)

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

        # Get slow bars for regime context
        slow_history = ctx.history(self.slow_interval)
        if len(slow_history) > 0:
            self.slow_close_history.append(slow_history[-1].close)

        # Position management
        if ctx.position:
            self.bars_held += 1
            # Only exit on sustained loss after holding for a while
            if self.bars_held > self.max_hold_bars and ctx.unrealized_pnl < 0:
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
            self.absorption_delta = fast_bar.delta
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
        
        Sell absorption: Large negative delta (sellers aggressive) but fails to
        make a new low relative to prior bar, AND closes above prior close.
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
                return "buy_absorption"

        # Sell absorption: large negative delta but fails to extend low
        if last.delta < -threshold:
            # Must fail to make new low AND close above prior close
            if last.low >= prior.low and last.close > prior.close:
                return "sell_absorption"

        return None

    def _detect_initiation(self, bar, absorption_signal: str) -> bool:
        """
        Detect initiation: opposite delta flow that efficiently moves price.
        
        Initiation must:
        1. Have opposite delta from absorption signal
        2. Exceed the threshold
        3. Move price in the direction of the delta
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta + closes low
            if bar.delta < -threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0:
                    # Close in lower half of range (price moved down with selling)
                    close_to_low = (bar.high - bar.close) / bar_range
                    if close_to_low > 0.5:
                        return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta + closes high
            if bar.delta > threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0:
                    # Close in upper half of range (price moved up with buying)
                    close_to_high = (bar.close - bar.low) / bar_range
                    if close_to_high > 0.5:
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
                tag="absorption-sell-init",
            )
        elif absorption_signal == "sell_absorption":
            # Absorption of selling = buy initiation
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-buy-init",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
