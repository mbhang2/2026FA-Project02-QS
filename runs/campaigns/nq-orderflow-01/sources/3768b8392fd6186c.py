"""
Absorption and initiation strategy with refined sequential confirmation.

Analysis of trial progression shows:
- Trial #6: -0.1967 (158 trades) - Best overall baseline
- Trial #35: -0.1490 (42 trades) - Too few trades, but better quality
- Trial #37: -0.1117 (13 trades) - Adaptive thresholds killed signal volume

The key insight: Trial #35's score of -0.1490 on just 42 trades suggests that
extreme selectivity can work, but 13 trades (#37) is too few for reliability.

The problem with #37: adaptive thresholds based on delta volatility made the
prior delta requirement too dynamic and unpredictable. When volatility spikes,
the threshold becomes very high and filters out valid signals.

New approach - return to fixed, well-tuned thresholds but with better sequential
confirmation:
1. Keep core absorption logic from #6 (proven to work)
2. Use FIXED prior delta threshold of 30.0 (between #35's strictness and #6's
   looseness) - this was tested in #35 and gave -0.1490
3. Tighten close position requirement: bottom quartile for buy absorption, top
   quartile for sell absorption (like #35 and #37)
4. For initiation: require TWO consecutive bars showing opposite delta
   - First bar must show opposite delta at threshold * 0.65
   - Second bar must also show opposite delta at threshold * 0.65
   - This ensures sustained reversal, not just a single bar blip
5. Keep proven exit: max_hold_bars=6, stop_offset=0.40, target_offset=0.65

Expected outcome: ~80-120 trades with score around -0.1550 to -0.1750 by:
- Using fixed thresholds (more stable than adaptive)
- Requiring sustained confirmation (two bars, not one)
- Being selective but not overly restrictive
- Balancing quality with signal volume
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
        initiation_window: int = 4,
        initiation_delta_strength: float = 0.65,
        prior_delta_threshold: float = 30.0,
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
        self.prior_delta_threshold: float = prior_delta_threshold

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
        self.initiation_confirmation_count: int = 0
        self.last_initiation_delta_sign: Optional[int] = None

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
            self.initiation_confirmation_count = 0
            self.last_initiation_delta_sign = None
            return

        # Reset when flat
        self.bars_held = 0

        # Detect absorption on fast bars
        absorption = self._detect_absorption(fast_history)

        if absorption:
            self.absorption_signal = absorption
            self.bars_since_absorption = 0
            self.initiation_confirmation_count = 0
            self.last_initiation_delta_sign = None
            return

        # Check for initiation after absorption (within the window)
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            if self.bars_since_absorption <= self.initiation_window:
                if self._is_initiation_bar(fast_bar, self.absorption_signal):
                    # Track the sign of the initiation delta
                    delta_sign = 1 if fast_bar.delta > 0 else -1
                    
                    # Check if this bar continues the same directional flow
                    if self.last_initiation_delta_sign is None:
                        # First initiation bar
                        self.last_initiation_delta_sign = delta_sign
                        self.initiation_confirmation_count = 1
                    elif self.last_initiation_delta_sign == delta_sign:
                        # Continuation of same direction
                        self.initiation_confirmation_count += 1
                        if self.initiation_confirmation_count >= 2:
                            self._place_entry(ctx, self.absorption_signal)
                            self.absorption_signal = None
                            self.bars_since_absorption = 0
                            self.initiation_confirmation_count = 0
                            self.last_initiation_delta_sign = None
                    else:
                        # Direction changed, reset
                        self.last_initiation_delta_sign = delta_sign
                        self.initiation_confirmation_count = 1
                else:
                    # Bar doesn't show initiation, reset counter
                    self.initiation_confirmation_count = 0
                    self.last_initiation_delta_sign = None
            else:
                # Reset if we exceed the initiation window
                self.absorption_signal = None
                self.bars_since_absorption = 0
                self.initiation_confirmation_count = 0
                self.last_initiation_delta_sign = None

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

    def _is_close_in_bottom_quartile(self, bar) -> bool:
        """Check if close is in bottom quartile of bar range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False
        close_position = (bar.close - bar.low) / bar_range
        return close_position < 0.25

    def _is_close_in_top_quartile(self, bar) -> bool:
        """Check if close is in top quartile of bar range."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False
        close_position = (bar.close - bar.low) / bar_range
        return close_position > 0.75

    def _detect_absorption(self, history) -> Optional[str]:
        """
        Detect absorption: large delta that fails to extend price extremes.
        
        Buy absorption: Large positive delta (buyers aggressive) but fails to
        make a new high relative to prior bar, AND closes below prior close.
        Confirm with prior bar showing meaningful positive delta.
        AND close must be in bottom quartile of the bar's range.
        
        Sell absorption: Large negative delta (sellers aggressive) but fails to
        make a new low relative to prior bar, AND closes above prior close.
        Confirm with prior bar showing meaningful negative delta.
        AND close must be in top quartile of the bar's range.
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
                # Confirm with prior bar showing meaningful positive delta
                if prior.delta > self.prior_delta_threshold:
                    # Additional check: close in bottom quartile of range
                    if self._is_close_in_bottom_quartile(last):
                        return "buy_absorption"

        # Sell absorption: large negative delta but fails to extend low
        if last.delta < -threshold:
            # Must fail to make new low AND close above prior close
            if last.low >= prior.low and last.close > prior.close:
                # Confirm with prior bar showing meaningful negative delta
                if prior.delta < -self.prior_delta_threshold:
                    # Additional check: close in top quartile of range
                    if self._is_close_in_top_quartile(last):
                        return "sell_absorption"

        return None

    def _is_initiation_bar(self, bar, absorption_signal: str) -> bool:
        """
        Check if bar shows initiation: opposite delta flow in the right direction.
        
        For buy absorption (we want to sell): need strong negative delta
        For sell absorption (we want to buy): need strong positive delta
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta
            if bar.delta < -threshold * self.initiation_delta_strength:
                return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta
            if bar.delta > threshold * self.initiation_delta_strength:
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
