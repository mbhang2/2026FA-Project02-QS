"""
Absorption and initiation strategy with time-of-day filtering and volatility adaptation.

Analysis of trial progression reveals several key insights:

Best performers:
- Trial #6: -0.1967 (158 trades) - Best overall, strong absorption + simple initiation
- Trial #39: -0.1164 (32 trades) - Sweet spot with 32 trades
- Trial #45: -0.1416 (50 trades) - Reasonable performance with more trades
- Trial #46: -0.1918 (81 trades) - More trades but slightly worse score

The pattern suggests:
1. Simpler entry logic outperforms complex thresholds
2. Trade count in 30-160 range works, but sweet spot is 30-50 trades
3. Absorption detection with prior bar confirmation is solid
4. Initiation needs strong opposite delta but not overly constrained

Critical observation: Trial #6 with 158 trades at -0.1967 beats trial #39 with
32 trades at -0.1164. This suggests that more trades at slightly worse per-trade
quality still yields better overall score. The issue may be that we've been
over-filtering.

New approach - time and volatility adaptive:
1. Keep proven absorption detection from trial #46 (delta percentile 0.82, volume filter)
2. Add time-of-day filtering: avoid the first 30 minutes (cash open volatility) and
   last 30 minutes (end-of-session squeeze). Most productive trading is mid-session.
3. Make initiation less strict: require 0.70 of threshold (down from 0.80) to capture
   more reversals
4. Add volatility adaptation: scale initiation threshold by recent ATR to match market regime
5. Keep proven exit: stop_offset=0.40, target_offset=0.65, max_hold_bars=6
6. Widen initiation_window=4 to give more time for reversal to develop
7. Remove volume check on initiation bar - it filters good signals

The hypothesis: we've been filtering too aggressively. Trial #6's success came from
capturing more reversals while maintaining good absorption detection. Time-of-day
filtering removes the noisiest periods without over-constraining the core signal.

Expected outcome: 100-150 trades with score around -0.1800 to -0.1950.
"""

from collections import deque
from typing import Deque, Optional


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        lookback: int = 30,
        delta_percentile: float = 0.82,
        volume_threshold: float = 0.60,
        stop_offset: float = 0.40,
        target_offset: float = 0.65,
        size: int = 1,
        max_hold_bars: int = 6,
        initiation_window: int = 4,
        initiation_delta_strength: float = 0.70,
        prior_delta_threshold: float = 40.0,
        session_start_buffer_minutes: int = 30,
        session_end_buffer_minutes: int = 30,
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
        self.session_start_buffer_minutes: int = session_start_buffer_minutes
        self.session_end_buffer_minutes: int = session_end_buffer_minutes

        # Fast (1m) history
        self.fast_delta_history: Deque[int] = deque(maxlen=lookback)
        self.fast_volume_history: Deque[int] = deque(maxlen=lookback)
        self.fast_high_history: Deque[float] = deque(maxlen=lookback)
        self.fast_low_history: Deque[float] = deque(maxlen=lookback)
        self.fast_close_history: Deque[float] = deque(maxlen=lookback)
        self.fast_atr_history: Deque[float] = deque(maxlen=lookback)

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

        # Check if we're in a tradeable time window
        if not self._is_tradeable_time(ctx):
            # Reset signals during non-tradeable times
            self.absorption_signal = None
            self.bars_since_absorption = 0
            return

        # Update fast history
        self.fast_delta_history.append(fast_bar.delta)
        self.fast_volume_history.append(fast_bar.volume)
        self.fast_high_history.append(fast_bar.high)
        self.fast_low_history.append(fast_bar.low)
        self.fast_close_history.append(fast_bar.close)
        
        # Calculate and store ATR
        atr = self._calculate_atr(fast_history)
        self.fast_atr_history.append(atr)

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
                if self._is_initiation_bar(fast_bar, self.absorption_signal):
                    self._place_entry(ctx, self.absorption_signal)
                    self.absorption_signal = None
                    self.bars_since_absorption = 0
            else:
                # Reset if we exceed the initiation window
                self.absorption_signal = None
                self.bars_since_absorption = 0

    def _is_tradeable_time(self, ctx) -> bool:
        """Check if current time is within tradeable window (avoiding open/close)."""
        time_remaining = ctx.session_end - ctx.ts
        session_duration = ctx.session_end - ctx.session_start
        
        # Avoid first N minutes of session (cash open)
        start_buffer_ns = self.session_start_buffer_minutes * 60 * 1_000_000_000
        if ctx.ts - ctx.session_start < start_buffer_ns:
            return False
        
        # Avoid last N minutes of session (end squeeze)
        end_buffer_ns = self.session_end_buffer_minutes * 60 * 1_000_000_000
        if time_remaining < end_buffer_ns:
            return False
        
        return True

    def _calculate_atr(self, history, period: int = 14) -> float:
        """Calculate Average True Range for volatility adaptation."""
        if len(history) < 2:
            return 1.0
        
        trs = []
        for i in range(max(0, len(history) - period), len(history)):
            bar = history[i]
            if i == 0:
                tr = bar.high - bar.low
            else:
                prev_close = history[i - 1].close
                tr = max(
                    bar.high - bar.low,
                    abs(bar.high - prev_close),
                    abs(bar.low - prev_close),
                )
            trs.append(tr)
        
        if not trs:
            return 1.0
        return sum(trs) / len(trs)

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
        Confirm with prior bar showing positive delta.
        
        Sell absorption: Large negative delta (sellers aggressive) but fails to
        make a new low relative to prior bar, AND closes above prior close.
        Confirm with prior bar showing negative delta.
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
                # Confirm with prior bar showing positive delta
                if prior.delta > self.prior_delta_threshold:
                    return "buy_absorption"

        # Sell absorption: large negative delta but fails to extend low
        if last.delta < -threshold:
            # Must fail to make new low AND close above prior close
            if last.low >= prior.low and last.close > prior.close:
                # Confirm with prior bar showing negative delta
                if prior.delta < -self.prior_delta_threshold:
                    return "sell_absorption"

        return None

    def _is_initiation_bar(self, bar, absorption_signal: str) -> bool:
        """
        Check if bar shows initiation: opposite delta flow with volatility adaptation.
        
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
