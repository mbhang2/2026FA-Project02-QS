"""
Volume profile absorption strategy - multi-bar confirmation with reduced frequency.

Analysis of trial #17 (best: score=-0.5016, 11,356 trades):
- Moderate frequency (~29 trades/session) showed improvement
- Absorption + confirmation pattern was directionally correct
- But still losing money, suggesting the pattern needs refinement

Key insight: The 2-bar confirmation pattern is too loose.
- Many bars close past a rejection level without real follow-through
- We need stronger evidence that absorption precedes a real move
- The campaign asks: does flow that fails to move price precede a move AWAY?

Critical realization from campaign brief:
- "A rule that acts a handful of times and closes the same day will not be measurable"
- Multi-session holds are REQUIRED to reach 70% floor
- We need ~1-2 trades per session ON AVERAGE that hold 2-5 sessions each
- This means we should trade LESS OFTEN but with higher conviction

New approach - stricter absorption + longer confirmation window:
1. Detect absorption: strong delta ratio (40%+) + meaningful wick + low momentum bar
2. Wait for 2-BAR confirmation: next bar must close decisively past absorption extreme
3. ALSO require volume confirmation: next bar should show elevated volume
4. Only then enter, with targets sized to move across multiple bars (4-5 points)
5. Stops at absorption bar extreme + small buffer (structural, not tuned)
6. This creates a 3-bar pattern (absorption + 2-bar confirmation) with better predictive power
7. Much lower frequency (maybe 5-10 trades total per session) but higher conviction

The hypothesis: absorption that REALLY precedes moves will show:
- Clear rejection bar (absorption)
- Clear follow-through bar (confirmation)
- Elevated volume on confirmation (not just price action)

This filters out noise and should reduce from 11k trades to 2-3k trades
while maintaining exposure through longer holds.
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback_bars: int = 20,
        delta_ratio_threshold: float = 0.40,
        min_volume: int = 80,
        stop_offset: float = 0.75,
        target_offset: float = 4.5,
        size: int = 1,
        wick_min_ratio: float = 0.20,
        confirmation_bars: int = 2,
        volume_spike_ratio: float = 1.2,
    ) -> None:
        self.interval = interval
        self.lookback_bars = lookback_bars
        self.delta_ratio_threshold = delta_ratio_threshold
        self.min_volume = min_volume
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.wick_min_ratio = wick_min_ratio
        self.confirmation_bars = confirmation_bars
        self.volume_spike_ratio = volume_spike_ratio

        # Track bars and pending absorption signals
        self.bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None
        # (direction, absorption_bar_high/low, bars_since_absorption, baseline_volume)
        self.pending_absorption: Optional[Tuple[str, float, int, float]] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            # Reset pending absorption at session boundary
            self.pending_absorption = None

        # Get current bar
        if self.interval not in ctx.bars:
            return

        current_bar = ctx.bars[self.interval]
        self.bars.append(current_bar)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Need minimum history
        if len(self.bars) < self.lookback_bars + 1:
            return

        # Check if we have a pending absorption signal waiting for confirmation
        if self.pending_absorption is not None:
            direction, reference_level, bars_since, baseline_vol = self.pending_absorption
            bars_since += 1

            # Check if confirmation criteria are met on this bar
            if self._check_multi_bar_confirmation(current_bar, direction, reference_level, baseline_vol):
                # Confirmed! Execute the trade
                tag = f"absorption-confirmed-{direction.lower()}"

                if direction == "BUY":
                    ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag=tag,
                    )
                else:
                    ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_offset,
                        target_offset=self.target_offset,
                        tag=tag,
                    )

                # Clear pending signal after trade
                self.pending_absorption = None
                return
            elif bars_since >= self.confirmation_bars:
                # Confirmation window closed without confirmation, discard signal
                self.pending_absorption = None
            else:
                # Still waiting for confirmation, update counter
                self.pending_absorption = (direction, reference_level, bars_since, baseline_vol)
                return

        # Look for new absorption signal on this bar
        absorption_info = self._detect_absorption(current_bar)
        if absorption_info is not None:
            direction, reference_level, baseline_vol = absorption_info

            # Store as pending - will confirm over next 1-2 bars
            self.pending_absorption = (direction, reference_level, 0, baseline_vol)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption(self, current_bar) -> Optional[Tuple[str, float, float]]:
        """
        Detect absorption: strong one-way flow with price rejection and low momentum.

        Returns (direction, reference_level, baseline_volume) or None.
        Direction is "BUY" or "SELL".
        Reference level is the extreme that was rejected (high for buy, low for sell).
        """
        if current_bar.volume < self.min_volume:
            return None

        # Calculate delta ratio
        delta_ratio = (
            abs(current_bar.delta) / current_bar.volume
            if current_bar.volume > 0
            else 0
        )

        # Need strong one-way flow
        if delta_ratio < self.delta_ratio_threshold:
            return None

        # Check wick formation (price rejection)
        bar_range = current_bar.high - current_bar.low
        if bar_range < 0.15:
            return None

        # Calculate momentum (how far bar moved in its direction)
        # Low momentum + strong delta = absorption (flow without price movement)
        momentum_ratio = abs(current_bar.close - current_bar.open) / bar_range if bar_range > 0 else 0

        # Upside absorption: positive delta with upper wick and low momentum
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Wick must be meaningful (price rejected upside)
            # Momentum should be low (flow without follow-through)
            if wick_ratio > self.wick_min_ratio and momentum_ratio < 0.50:
                # Close should be higher than open (bullish bar despite wick)
                if current_bar.close >= current_bar.open:
                    # Return the high that was rejected as reference level
                    return ("BUY", current_bar.high, current_bar.volume)

        # Downside absorption: negative delta with lower wick and low momentum
        elif current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Wick must be meaningful (price rejected downside)
            # Momentum should be low (flow without follow-through)
            if wick_ratio > self.wick_min_ratio and momentum_ratio < 0.50:
                # Close should be lower than open (bearish bar despite wick)
                if current_bar.close <= current_bar.open:
                    # Return the low that was rejected as reference level
                    return ("SELL", current_bar.low, current_bar.volume)

        return None

    def _check_multi_bar_confirmation(
        self, current_bar, direction: str, reference_level: float, baseline_vol: float
    ) -> bool:
        """
        Check if current bar confirms the absorption signal.

        Confirmation requires:
        1. Price closes decisively past the absorption extreme
        2. Volume is elevated (at least 1.2x baseline)
        3. Momentum is in the right direction
        """
        # Check volume spike (must show elevated activity)
        if current_bar.volume < baseline_vol * self.volume_spike_ratio:
            return False

        bar_range = current_bar.high - current_bar.low
        if bar_range <= 0:
            return False

        if direction == "BUY":
            # Confirmation: close above the rejected high with volume
            # AND momentum should be bullish
            if current_bar.close > reference_level:
                momentum_ratio = (current_bar.close - current_bar.open) / bar_range
                # Momentum should be positive (bullish follow-through)
                if momentum_ratio > 0.10:
                    return True

        elif direction == "SELL":
            # Confirmation: close below the rejected low with volume
            # AND momentum should be bearish
            if current_bar.close < reference_level:
                momentum_ratio = (current_bar.close - current_bar.open) / bar_range
                # Momentum should be negative (bearish follow-through)
                if momentum_ratio < -0.10:
                    return True

        return False
