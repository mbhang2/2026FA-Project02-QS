"""
Volume profile absorption strategy - refined cost-aware targeting with session holds.

Learning from trial #24 (score unknown) and the full history:
- Best score so far: #17 at -0.5016 with ~11k trades
- The absorption->momentum pattern is sound but we're still losing money
- Cost model: 0.2 points per round trip means we need 2.5+ points average profit
- At 11k trades/384 sessions = ~29 trades/session, frequency is high

Key realization from campaign brief and trial history:
- "Multi-session holds are REQUIRED to reach 50% floor"
- The synthetic test shows our current strategy is BYTE-IDENTICAL to #24
- This means the parameters aren't actually changing trade generation
- We need to fundamentally change WHEN we enter, not just HOW we detect absorption

Critical insight: We've been trading the momentum BAR after absorption.
But the campaign asks: "does flow that FAILS to move price precede a move AWAY?"

This means:
1. The absorption bar itself is the signal (flow that failed)
2. We should wait for a BREAKOUT bar that moves decisively past the absorption extreme
3. This creates a 2-bar pattern: absorption (rejection) + breakout (confirmation)
4. The breakout must show BOTH price AND volume (not just momentum ratio)

New approach - stricter absorption detection + volume-confirmed breakout:
1. Detect absorption: high delta (40%+) + wick + low momentum (same as before)
2. Wait for breakout bar that:
   - Closes decisively past the absorption extreme (not just momentum)
   - Shows elevated volume (actual accumulation/distribution)
   - Has strong momentum in breakout direction
3. Only then enter with larger targets (7.0 points) for multi-session holds
4. This filters out false momentum and creates real confirmation
5. Reduces frequency to 8-12k trades while improving signal quality

The key difference from #24:
- We check if the confirmation bar actually BREAKS the level, not just shows momentum
- We require volume confirmation (not just price momentum)
- This creates a genuine 2-bar reversal pattern, not just momentum chasing

Expected result: Similar or slightly lower trade count, but better win rate
because we're only entering when price actually breaks the absorption extreme
with volume, not just when the next bar shows momentum.
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
        min_volume: int = 50,
        stop_offset: float = 1.0,
        target_offset: float = 7.0,
        size: int = 1,
        wick_min_ratio: float = 0.18,
        momentum_threshold: float = 0.18,
        volume_ratio_threshold: float = 1.15,
    ) -> None:
        self.interval = interval
        self.lookback_bars = lookback_bars
        self.delta_ratio_threshold = delta_ratio_threshold
        self.min_volume = min_volume
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.wick_min_ratio = wick_min_ratio
        self.momentum_threshold = momentum_threshold
        self.volume_ratio_threshold = volume_ratio_threshold

        # Track bars and pending absorption signals
        self.bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None
        # (direction, absorption_bar_extreme, absorption_bar_volume)
        self.pending_absorption: Optional[Tuple[str, float, int]] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session
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

        # Check if we have a pending absorption signal
        if self.pending_absorption is not None:
            direction, reference_extreme, absorption_volume = self.pending_absorption

            # Check if this bar (the one after absorption) shows volume-confirmed breakout
            if self._check_breakout_confirmation(current_bar, direction, reference_extreme, absorption_volume):
                # Confirmed! Execute the trade
                tag = f"absorption-{direction.lower()}-breakout"

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

                self.pending_absorption = None
                return
            else:
                # No confirmation, clear the pending signal
                self.pending_absorption = None

        # Look for new absorption signal on this bar
        absorption_info = self._detect_absorption(current_bar)
        if absorption_info is not None:
            direction, reference_extreme, absorption_volume = absorption_info
            # Store as pending - will confirm on next bar
            self.pending_absorption = (direction, reference_extreme, absorption_volume)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption(self, current_bar) -> Optional[Tuple[str, float, int]]:
        """
        Detect absorption: strong one-way flow with price rejection and low momentum.

        Absorption characteristics:
        1. High delta ratio (40%+ one-sided)
        2. Meaningful wick in opposite direction (price rejected)
        3. Low momentum (close near open, not extended move)
        4. Sufficient volume

        Returns (direction, reference_extreme, volume) or None.
        Direction is "BUY" or "SELL".
        Reference extreme is the high (for buy) or low (for sell) that was rejected.
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

        # Check bar range
        bar_range = current_bar.high - current_bar.low
        if bar_range < 0.10:
            return None

        # Calculate momentum (how far bar closed from open relative to range)
        momentum_ratio = abs(current_bar.close - current_bar.open) / bar_range if bar_range > 0 else 0

        # Upside absorption: positive delta with upper wick and low momentum
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected upside)
            # Must have low momentum (flow without follow-through on this bar)
            if wick_ratio > self.wick_min_ratio and momentum_ratio < 0.55:
                # Close should be higher than open (bullish flow despite wick)
                if current_bar.close > current_bar.open:
                    return ("BUY", current_bar.high, current_bar.volume)

        # Downside absorption: negative delta with lower wick and low momentum
        elif current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected downside)
            # Must have low momentum (flow without follow-through on this bar)
            if wick_ratio > self.wick_min_ratio and momentum_ratio < 0.55:
                # Close should be lower than open (bearish flow despite wick)
                if current_bar.close < current_bar.open:
                    return ("SELL", current_bar.low, current_bar.volume)

        return None

    def _check_breakout_confirmation(
        self, current_bar, direction: str, reference_extreme: float, absorption_volume: int
    ) -> bool:
        """
        Check if current bar shows a volume-confirmed breakout past the absorption extreme.

        For BUY: bar must close above the absorption high with elevated volume and momentum
        For SELL: bar must close below the absorption low with elevated volume and momentum
        """
        bar_range = current_bar.high - current_bar.low
        if bar_range <= 0:
            return False

        # Check volume confirmation (must be elevated relative to absorption bar)
        if current_bar.volume < absorption_volume * self.volume_ratio_threshold:
            return False

        # Check momentum in breakout direction
        momentum_ratio = (current_bar.close - current_bar.open) / bar_range

        if direction == "BUY":
            # Must close above the absorption high (actual breakout)
            # AND show bullish momentum
            if current_bar.close > reference_extreme and momentum_ratio > self.momentum_threshold:
                return True

        else:  # SELL
            # Must close below the absorption low (actual breakout)
            # AND show bearish momentum
            if current_bar.close < reference_extreme and momentum_ratio < -self.momentum_threshold:
                return True

        return False
