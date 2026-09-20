"""
Absorption and initiation strategy with adaptive multi-bar confirmation and
volatility-adjusted thresholds.

Analysis of trial progression reveals key patterns:

Best performers:
- Trial #6: -0.1967 (158 trades) - Best overall, strong signal volume
- Trial #48: -0.0875 (16 trades) - Excellent efficiency but too few trades
- Trial #47: -0.2162 (103 trades) - Good with time filtering
- Trial #45: -0.1416 (50 trades) - Solid mid-range performance

Critical insight: Trial #6's 158 trades at -0.1967 beats trial #48's 16 trades
at -0.0875. This shows that more trades at slightly worse per-trade quality
yields better overall score. The signal is robust and should be scaled, not
filtered down.

However, trials #47-#48 show that time filtering and stricter confirmation
both degraded performance. This suggests:
1. The original absorption detection is solid
2. Initiation confirmation needs to be less restrictive
3. We're losing good signal by over-filtering

New approach - volatility-adaptive initiation with relaxed confirmation:
1. Keep absorption detection from #6: simple delta percentile threshold
2. Remove prior bar delta confirmation (was too restrictive)
3. Simplify initiation: just require opposite strong delta, no multi-bar requirement
4. Add volatility adaptation: scale delta threshold by recent volatility
5. Use ATR to normalize delta expectations in different market regimes
6. Keep proven exits: stop_offset=0.40, target_offset=0.65, max_hold_bars=6
7. Reduce initiation_delta_strength to 0.60 (capture more reversals)
8. Single-bar initiation entry (don't wait for confirmation bars)
9. Wider initiation_window=5 to give more time for reversal

The hypothesis: Trial #6 worked because it was simple and captured the signal
without over-filtering. By removing multi-bar confirmation requirements and
using volatility-adaptive thresholds instead of fixed ones, we should recover
the signal volume while maintaining selectivity.

Expected outcome: 140-180 trades with score around -0.1900 to -0.2000.
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
        initiation_window: int = 5,
        initiation_delta_strength: float = 0.60,
        atr_period: int = 14,
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
        self.atr_period: int = atr_period

        # Fast (1m) history
        self.fast_delta_history: Deque[int] = deque(maxlen=lookback)
        self.fast_volume_history: Deque[int] = deque(maxlen=lookback)
        self.fast_high_history: Deque[float] = deque(maxlen=lookback)
        self.fast_low_history: Deque[float] = deque(maxlen=lookback)
        self.fast_close_history: Deque[float] = deque(maxlen=lookback)
        self.fast_tr_history: Deque[float] = deque(maxlen=atr_period)

        # Signal tracking
        self.absorption_signal: Optional[str] = None
        self.bars_since_absorption: int = 0
        self.bars_held: int = 0
        self.last_close: Optional[float] = None

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

        # Calculate True Range for volatility
        if self.last_close is not None:
            tr = max(
                fast_bar.high - fast_bar.low,
                abs(fast_bar.high - self.last_close),
                abs(fast_bar.low - self.last_close),
            )
            self.fast_tr_history.append(tr)
        else:
            self.fast_tr_history.append(fast_bar.high - fast_bar.low)
        
        self.last_close = fast_bar.close

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

    def _get_atr(self) -> float:
        """Calculate Average True Range for volatility normalization."""
        if len(self.fast_tr_history) < self.atr_period:
            if not self.fast_tr_history:
                return 1.0
            return sum(self.fast_tr_history) / len(self.fast_tr_history)
        return sum(list(self.fast_tr_history)[-self.atr_period:]) / self.atr_period

    def _get_delta_threshold(self) -> float:
        """
        Compute delta threshold using percentile of recent absolute deltas,
        normalized by volatility.
        """
        if len(self.fast_delta_history) < self.lookback // 2:
            return 50.0

        abs_deltas = sorted([abs(d) for d in self.fast_delta_history])
        idx = int(len(abs_deltas) * self.delta_percentile)
        idx = max(0, min(idx, len(abs_deltas) - 1))
        threshold: float = float(abs_deltas[idx])

        # Volatility normalization: scale threshold by ATR
        atr = self._get_atr()
        if atr > 0:
            # In high volatility, expect larger deltas; scale threshold up
            threshold = threshold * (atr / 0.5)  # 0.5 as baseline ATR

        return max(threshold, 40.0)

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

    def _is_initiation_bar(self, bar, absorption_signal: str) -> bool:
        """
        Check if bar shows initiation: opposite delta flow.
        
        For buy absorption (we want to sell): need strong negative delta
        For sell absorption (we want to buy): need strong positive delta
        """
        threshold = self._get_delta_threshold()
        initiation_threshold = threshold * self.initiation_delta_strength

        if absorption_signal == "buy_absorption":
            # Look for sell initiation: strong negative delta
            if bar.delta < -initiation_threshold:
                return True

        elif absorption_signal == "sell_absorption":
            # Look for buy initiation: strong positive delta
            if bar.delta > initiation_threshold:
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
