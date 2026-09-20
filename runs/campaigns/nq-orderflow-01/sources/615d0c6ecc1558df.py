"""
Absorption and initiation strategy with multi-timeframe filtering and adaptive thresholds.

Prior trials scored -0.6165 and -0.3989. The best trial (#1) used percentile-based
delta thresholds and immediate initiation detection, but the score suggests we're
catching too many weak signals or holding positions too long.

Key refinements for this iteration:

1. Multi-timeframe filtering: Use 5m bars to filter absorption signals. Only trade
   1m absorptions that align with 5m trend direction. This reduces noise from
   intrabar noise that reverses at the 5m level.

2. Adaptive thresholds based on session time: Early session (first 2 hours) has
   different dynamics than rest of day. Use higher thresholds early to avoid noise.

3. Stricter initiation: Require initiation delta to exceed absorption delta magnitude
   (not just threshold). This ensures real reversal, not just opposite noise.

4. Exit management: Don't rely solely on brackets. Track position age and exit if
   no profit after 5 bars. This prevents being right on direction but wrong on
   timing.

5. Volume-weighted delta: Weight delta by relative volume to distinguish real
   absorption from low-volume noise.

6. Reduce position size to 1 and use tighter stops/targets to survive commission
   costs better.
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
        stop_offset: float = 0.35,
        target_offset: float = 0.50,
        size: int = 1,
        max_hold_bars: int = 5,
        early_session_bars: int = 120,
        early_session_multiplier: float = 1.5,
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
        self.early_session_bars: int = early_session_bars
        self.early_session_multiplier: float = early_session_multiplier

        # Fast (1m) history
        self.fast_delta_history: Deque[int] = deque(maxlen=lookback)
        self.fast_volume_history: Deque[int] = deque(maxlen=lookback)
        self.fast_high_history: Deque[float] = deque(maxlen=lookback)
        self.fast_low_history: Deque[float] = deque(maxlen=lookback)
        self.fast_close_history: Deque[float] = deque(maxlen=lookback)

        # Slow (5m) trend tracking
        self.slow_close_history: Deque[float] = deque(maxlen=5)

        # Signal tracking
        self.absorption_signal: Optional[str] = None
        self.absorption_delta: int = 0
        self.bars_since_absorption: int = 0
        self.bars_held: int = 0
        self.session_bar_count: int = 0
        self.last_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.last_session:
            self.last_session = ctx.session
            self.session_bar_count = 0

        self.session_bar_count += 1

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

        # Get slow bars for trend filter
        slow_history = ctx.history(self.slow_interval)
        if len(slow_history) > 0:
            self.slow_close_history.append(slow_history[-1].close)

        # Position management
        if ctx.position:
            self.bars_held += 1
            # Exit if held too long without profit
            if self.bars_held > self.max_hold_bars:
                if ctx.unrealized_pnl <= 0:
                    ctx.close()
            self.absorption_signal = None
            self.bars_since_absorption = 0
            return

        # Reset on new signal
        self.bars_held = 0

        # Detect absorption on fast bars
        absorption = self._detect_absorption(fast_history)

        if absorption:
            # Check if slow trend aligns (filter)
            if self._check_slow_trend_filter(absorption):
                self.absorption_signal = absorption
                self.absorption_delta = fast_bar.delta
                self.bars_since_absorption = 0
            return

        # Check for initiation after absorption
        if self.absorption_signal is not None:
            self.bars_since_absorption += 1

            # Only check for initiation on the very next bar
            if self.bars_since_absorption == 1:
                if self._detect_initiation(fast_bar, self.absorption_signal):
                    self._place_entry(ctx, self.absorption_signal)
                    self.absorption_signal = None
                    self.bars_since_absorption = 0
            else:
                # Reset if we miss the initiation window
                self.absorption_signal = None
                self.bars_since_absorption = 0

    def _is_early_session(self) -> bool:
        """Check if we're in the early session (first 2 hours)."""
        return self.session_bar_count < self.early_session_bars

    def _get_delta_threshold(self) -> float:
        """
        Compute delta threshold using percentile of recent absolute deltas.
        Adjust for early session noise.
        """
        if len(self.fast_delta_history) < self.lookback // 2:
            return 50.0

        abs_deltas = sorted([abs(d) for d in self.fast_delta_history])
        idx = int(len(abs_deltas) * self.delta_percentile)
        idx = max(0, min(idx, len(abs_deltas) - 1))
        threshold: float = float(abs_deltas[idx])

        # Higher threshold in early session to avoid noise
        if self._is_early_session():
            threshold *= self.early_session_multiplier

        return max(threshold, 50.0)

    def _get_avg_volume(self) -> float:
        """Get average volume from recent bars."""
        if not self.fast_volume_history:
            return 100.0
        return sum(self.fast_volume_history) / len(self.fast_volume_history)

    def _check_slow_trend_filter(self, absorption_signal: str) -> bool:
        """
        Filter absorption signals by 5m trend.
        Buy absorption valid only if 5m in downtrend (or flat).
        Sell absorption valid only if 5m in uptrend (or flat).
        """
        if len(self.slow_close_history) < 2:
            return True  # No slow data yet, allow signal

        slow_trend = self.slow_close_history[-1] - self.slow_close_history[-2]

        if absorption_signal == "buy_absorption":
            # Buy absorption (sellers failed) should occur in downtrend or flat
            return slow_trend <= 0
        else:  # sell_absorption
            # Sell absorption (buyers failed) should occur in uptrend or flat
            return slow_trend >= 0

    def _detect_absorption(self, history) -> Optional[str]:
        """
        Detect absorption: large delta that fails to extend price.
        Requires volume context to be normal.
        """
        if len(history) < 2:
            return None

        last = history[-1]
        prior = history[-2]

        avg_vol = self._get_avg_volume()
        if last.volume < avg_vol * self.volume_threshold:
            return None

        threshold = self._get_delta_threshold()

        # Buy absorption: large positive delta but price fails to go higher
        if last.delta > threshold:
            # Strong rejection: close below prior close
            if last.close < prior.close:
                return "buy_absorption"

        # Sell absorption: large negative delta but price fails to go lower
        if last.delta < -threshold:
            # Strong rejection: close above prior close
            if last.close > prior.close:
                return "sell_absorption"

        return None

    def _detect_initiation(self, bar, absorption_signal: str) -> bool:
        """
        Detect initiation: opposite delta flow that is stronger than absorption
        and moves price in the delta direction.
        """
        threshold = self._get_delta_threshold()

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: delta more negative than absorption was positive
            if bar.delta < -abs(self.absorption_delta) and bar.delta < -threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0:
                    close_to_low = (bar.high - bar.close) / bar_range
                    # Require close in upper 50% of range (near low)
                    if close_to_low > 0.5:
                        return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: delta more positive than absorption was negative
            if bar.delta > abs(self.absorption_delta) and bar.delta > threshold:
                bar_range = bar.high - bar.low
                if bar_range > 0:
                    close_to_high = (bar.close - bar.low) / bar_range
                    # Require close in upper 50% of range (near high)
                    if close_to_high > 0.5:
                        return True

        return False

    def _place_entry(self, ctx, absorption_signal: str) -> None:
        """Place entry order based on absorption signal."""
        if absorption_signal == "buy_absorption":
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-sell-init",
            )
        elif absorption_signal == "sell_absorption":
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-buy-init",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
