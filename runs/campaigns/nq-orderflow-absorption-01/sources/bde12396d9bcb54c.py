"""
Volume profile absorption strategy - focus on multi-session holds and cost survival.

Learning from trial #17 (score=-0.5016, 11,356 trades):
- Best performer so far at -0.5016, but still deeply negative
- ~30 trades per session is sustainable frequency
- The issue: even with absorption filtering, costs dominate at this frequency

Key insight from cost structure:
- Cost model: 0.2 points per round trip (0.1 commission + spread each way)
- At 11k trades across 384 sessions: average 28.6 trades/session
- Each trade needs 2+ points profit to break even on costs alone
- Trial #17's 7.0 target was still insufficient - suggests targets must be MUCH larger

The real constraint from campaign brief:
- "A rule that acts a handful of times and closes the same day will not be measurable"
- Multi-session holds are REQUIRED to reach 50% floor (192 sessions)
- Need to hold positions for 5-10+ bars to capture moves between levels
- This means fewer entries (maybe 5-10 per session) but much longer holds

New approach - radical shift to position holding:
1. Trade ONLY the clearest absorption signals (delta ratio > 50%)
2. Require price at exact level (not proximity) - very selective
3. Use MASSIVE targets (10+ points) - we're holding for real moves
4. Structural stops at 0.75 (tight relative to large target)
5. This generates ~5-10 trades per session but each holds 5-10 bars
6. Exposure stays high (270+ sessions) due to multi-bar holds
7. Cost per trade is amortized over larger moves

The hypothesis: if absorption truly precedes moves BETWEEN levels, then
holding for 10-point moves is the right timeframe, not scalping 3-point moves.
This tests the real hypothesis instead of a cost-adjusted version of it.

Implementation changes from previous:
- Increase target_offset from 7.0 to 10.0 (hold for real moves)
- Increase absorption_delta_ratio from 0.40 to 0.50 (only clearest signals)
- Require exact level match (level_proximity 0.15 instead of 0.25)
- Require wick to be 25%+ of bar range (was 20%)
- This should generate ~5-10 trades per session but with much larger profit targets
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        bar_interval: str = "1s",
        decision_interval: str = "5m",
        lookback_bars: int = 5,
        absorption_delta_ratio: float = 0.50,
        volume_spike_ratio: float = 1.4,
        stop_offset: float = 0.75,
        target_offset: float = 10.0,
        size: int = 1,
        min_volume: int = 50,
        wick_ratio_threshold: float = 0.25,
        level_proximity: float = 0.15,
        min_bar_range: float = 0.25,
    ) -> None:
        self.bar_interval = bar_interval
        self.decision_interval = decision_interval
        self.lookback_bars = lookback_bars
        self.absorption_delta_ratio = absorption_delta_ratio
        self.volume_spike_ratio = volume_spike_ratio
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.wick_ratio_threshold = wick_ratio_threshold
        self.level_proximity = level_proximity
        self.min_bar_range = min_bar_range

        # Track bars and levels
        self.decision_bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None
        self.support_levels: deque = deque(maxlen=100)
        self.resistance_levels: deque = deque(maxlen=100)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.bar_interval)
        ctx.subscribe(self.decision_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session

        # Make decisions on decision interval bars
        if self.decision_interval not in ctx.bars:
            return

        current_bar = ctx.bars[self.decision_interval]
        self.decision_bars.append(current_bar)

        # Update support/resistance levels from recent bars
        self._update_levels()

        # Skip if already in position - let positions run to target
        if ctx.position or ctx.working_order_ids:
            return

        # Need minimum history
        if len(self.decision_bars) < self.lookback_bars + 1:
            return

        # Detect absorption signal with very strict criteria
        signal = self._detect_absorption(current_bar)
        if signal is None:
            return

        direction, absorption_type = signal

        tag = f"absorption-{absorption_type}"

        if direction == Side.BUY:
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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_levels(self) -> None:
        """Update support/resistance levels from recent bars."""
        if len(self.decision_bars) < 2:
            return

        recent = list(self.decision_bars)[-self.lookback_bars :]

        # Find local extremes
        for i, bar in enumerate(recent):
            is_local_high = True
            is_local_low = True

            if i > 0:
                if recent[i - 1].high >= bar.high:
                    is_local_high = False
                if recent[i - 1].low <= bar.low:
                    is_local_low = False

            if i < len(recent) - 1:
                if recent[i + 1].high >= bar.high:
                    is_local_high = False
                if recent[i + 1].low <= bar.low:
                    is_local_low = False

            if is_local_high:
                self.resistance_levels.append(bar.high)
            if is_local_low:
                self.support_levels.append(bar.low)

    def _is_near_level(self, price: float) -> bool:
        """Check if price is near a support/resistance level (strict proximity)."""
        for level in self.resistance_levels:
            if abs(price - level) < self.level_proximity:
                return True

        for level in self.support_levels:
            if abs(price - level) < self.level_proximity:
                return True

        return False

    def _detect_absorption(self, current_bar) -> Optional[Tuple[Side, str]]:
        """
        Detect absorption with very strict criteria to ensure only highest-conviction trades:
        1. Very high delta ratio (50%+) - almost all flow one direction
        2. Volume spike (current > recent average * 1.4) - elevated activity
        3. Meaningful bar range (0.25+) - actual price movement
        4. Clear wick formation (25%+ of range) - strong price rejection
        5. Price exactly at a level (proximity 0.15) - not just near
        6. Price didn't extend far from prior bar - failed breakout attempt
        
        This generates fewer trades but only the highest-conviction absorption signals.
        """
        if current_bar.volume < self.min_volume:
            return None

        # Calculate delta ratio
        delta_ratio = (
            abs(current_bar.delta) / current_bar.volume
            if current_bar.volume > 0
            else 0
        )

        # Need VERY strong one-way flow (50%+ of volume)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check volume spike - elevated activity at this level
        if len(self.decision_bars) >= 2:
            recent_volumes = [b.volume for b in list(self.decision_bars)[-self.lookback_bars : -1]]
            avg_volume = sum(recent_volumes) / len(recent_volumes) if recent_volumes else 0
            if avg_volume > 0 and current_bar.volume < avg_volume * self.volume_spike_ratio:
                return None

        # Must be exactly at a level (strict proximity)
        if not self._is_near_level(current_bar.close):
            return None

        # Get prior bar for comparison
        if len(self.decision_bars) < 2:
            return None

        prev_bar = self.decision_bars[-2]
        bar_range = current_bar.high - current_bar.low

        # Bar must have meaningful range
        if bar_range < self.min_bar_range:
            return None

        # Upside absorption: positive delta with upper wick (rejection of higher prices)
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Wick must be very clear (25%+) and price didn't extend far above prior bar
            if (
                wick_ratio > self.wick_ratio_threshold
                and current_bar.high <= prev_bar.high + 0.5
            ):
                return (Side.BUY, "upside")

        # Downside absorption: negative delta with lower wick (rejection of lower prices)
        elif current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Wick must be very clear (25%+) and price didn't extend far below prior bar
            if (
                wick_ratio > self.wick_ratio_threshold
                and current_bar.low >= prev_bar.low - 0.5
            ):
                return (Side.SELL, "downside")

        return None
