"""
Volume profile absorption strategy - focus on absorption quality and cost efficiency.

Analysis of trial history and synthetic test results:
- Best score: #29 at -0.2654 with 743 trades (extreme selectivity)
- #17 at -0.5016 with 11,356 trades (good balance)
- Current approach: multi-bar confirmation with level testing

The synthetic test shows IDENTICAL trades to #31, which means:
1. The level testing logic (_is_at_significant_level) is not filtering effectively
2. The persistence confirmation is either always passing or always failing
3. We need to simplify and focus on what actually works

Root cause: Over-engineering with multiple confirmation layers that don't interact
properly. The level testing counts touches but doesn't validate they're at resistance/support.
The persistence check requires delta confirmation, but delta varies naturally.

New approach - return to proven mechanics with better filtering:
1. Keep absorption detection (high delta + wick rejection) - it works
2. Remove the complex level testing (it's not filtering in synthetic)
3. Remove the persistence delta requirement (too restrictive)
4. Instead: Use SIMPLER persistence - just require the move continues past extreme
5. Add one key filter: only trade absorption if it occurs AFTER a recent reversal
   (i.e., price tried to go the other way and failed)

This tests the core hypothesis from the brief:
"Does absorption at a carried level precede a move away from that level?"

The key insight: Absorption is meaningful when it represents REJECTION of a move.
If price just keeps going in one direction and we see high delta, that's momentum.
If price tries to go one way, gets rejected (wick), and then goes the other way,
that's absorption at a level.

So we filter by: did the PREVIOUS bar try to move opposite to the absorption?
- For upside absorption: previous bar should have had a lower close (tried down, failed)
- For downside absorption: previous bar should have had a higher close (tried up, failed)

This is the missing piece that explains why #29 (743 trades) worked better than
#31 (likely 8-10k trades). #29 had level proximity filtering which naturally
selected absorption that occurred at tested levels. We can approximate that with
a simpler "reversal context" check.

Expected impact:
- Trades: 1500-3000 (between #29's 743 and #17's 11k)
- Better signal quality (only absorption that follows rejection)
- Better score (filtering out false absorption in momentum phases)
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        delta_ratio_threshold: float = 0.25,
        min_volume: int = 40,
        stop_offset: float = 1.50,
        target_offset: float = 6.0,
        size: int = 1,
        wick_min_ratio: float = 0.15,
        momentum_max_ratio: float = 0.55,
    ) -> None:
        self.interval = interval
        self.delta_ratio_threshold = delta_ratio_threshold
        self.min_volume = min_volume
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.wick_min_ratio = wick_min_ratio
        self.momentum_max_ratio = momentum_max_ratio

        # Track bars and pending absorption signals
        self.bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None
        # (direction, absorption_extreme)
        self.pending_absorption: Optional[Tuple[str, float]] = None

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

        # Check if we have a pending absorption signal
        if self.pending_absorption is not None:
            direction, absorption_extreme = self.pending_absorption

            # Check if this bar breaks out past the absorption extreme
            if self._check_breakout(current_bar, direction, absorption_extreme):
                # Confirmed! Execute the trade on the breakout bar
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
                # No breakout confirmation, clear the pending signal
                self.pending_absorption = None

        # Look for new absorption signal on this bar
        absorption_direction = self._detect_absorption(current_bar)
        if absorption_direction is not None:
            direction = absorption_direction
            
            # Filter: only trade absorption if it follows a reversal attempt
            if len(self.bars) >= 2:
                prev_bar = self.bars[-2]
                if not self._is_reversal_context(prev_bar, direction):
                    return
            
            # Store the absorption extreme as reference for next bar's breakout
            if direction == "BUY":
                reference_extreme = current_bar.high
            else:
                reference_extreme = current_bar.low

            self.pending_absorption = (direction, reference_extreme)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption(self, current_bar) -> Optional[str]:
        """
        Detect absorption: strong one-way flow with price rejection and low momentum.

        Absorption characteristics:
        1. Delta ratio above threshold (25%+ one-sided)
        2. Meaningful wick in opposite direction (price rejected)
        3. Low momentum (close near open, not extended move)
        4. Sufficient volume

        Returns "BUY" or "SELL" if absorption detected, None otherwise.
        """
        if current_bar.volume < self.min_volume:
            return None

        # Calculate delta ratio
        delta_ratio = (
            abs(current_bar.delta) / current_bar.volume
            if current_bar.volume > 0
            else 0
        )

        # Need sufficient one-way flow
        if delta_ratio < self.delta_ratio_threshold:
            return None

        # Check bar range
        bar_range = current_bar.high - current_bar.low
        if bar_range < 0.10:
            return None

        # Calculate momentum (how far bar closed from open relative to range)
        momentum_ratio = (
            abs(current_bar.close - current_bar.open) / bar_range
            if bar_range > 0
            else 0
        )

        # Momentum must be LOW (flow without follow-through on this bar)
        if momentum_ratio > self.momentum_max_ratio:
            return None

        # Upside absorption: positive delta with upper wick
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected upside)
            if wick_ratio > self.wick_min_ratio:
                # Close should be higher than open (bullish flow despite wick)
                if current_bar.close > current_bar.open:
                    return "BUY"

        # Downside absorption: negative delta with lower wick
        elif current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected downside)
            if wick_ratio > self.wick_min_ratio:
                # Close should be lower than open (bearish flow despite wick)
                if current_bar.close < current_bar.open:
                    return "SELL"

        return None

    def _check_breakout(
        self, current_bar, direction: str, absorption_extreme: float
    ) -> bool:
        """
        Check if current bar breaks out past the absorption extreme.

        For BUY: bar must close above the absorption high
        For SELL: bar must close below the absorption low
        """
        if direction == "BUY":
            return current_bar.close > absorption_extreme
        else:
            return current_bar.close < absorption_extreme

    def _is_reversal_context(self, prev_bar, direction: str) -> bool:
        """
        Check if the previous bar represents an attempt in the opposite direction.
        
        This filters for absorption that occurs after price rejection:
        - For BUY absorption: previous bar should show downward bias (close < open)
        - For SELL absorption: previous bar should show upward bias (close > open)
        
        This ensures we're trading absorption at levels, not just high-delta bars
        in the middle of a trending move.
        """
        if direction == "BUY":
            # Previous bar tried to go down (close below open)
            return prev_bar.close < prev_bar.open
        else:
            # Previous bar tried to go up (close above open)
            return prev_bar.close > prev_bar.open
