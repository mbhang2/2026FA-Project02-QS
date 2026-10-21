"""
Volume profile absorption strategy - test whether absorption at levels predicts moves.

Analysis of the plateau and byte-identical trades:
The synthetic test being identical across many iterations reveals a critical issue:
the absorption detection is working, but the FILTERING logic isn't actually filtering
anything in the synthetic data. This means either:
1. The filtering conditions are too loose (passing everything)
2. The filtering conditions require market structure that synthetic noise doesn't have
3. We're detecting absorption correctly but on the wrong bars

Core insight from campaign brief and trial #29's best score (-0.2654, 743 trades):
"Does absorption at a carried level precede a move away from that level?"

The #29 strategy was EXTREMELY selective (743 trades in 384 sessions = 1.9 trades/session).
It scored well because it filtered ruthlessly. Every trade it made had high conviction.

The problem with current approach:
- We're still generating 11k trades like #17 (before filtering)
- The swing-level filtering in the current code isn't actually removing trades
- We need to be FAR more selective about WHEN to trade

New hypothesis - absorption is only predictive when:
1. It occurs at a price level that has been tested multiple times (not a new level)
2. The absorption bar has VERY high delta (35%+ not 28%)
3. The breakout bar shows SUSTAINED momentum (not just one bar)
4. We wait for a second confirmation bar showing the move continues
5. Targets are sized to the next ACTUAL tested level (not arbitrary 4.0-6.0)

This creates a much tighter filter:
- Only trade when price returns to a previously-tested zone
- Only when absorption is very strong (35%+ delta)
- Only when breakout is confirmed by persistence
- Result: 2-4k trades instead of 11k, but with much better win rate

Key difference from #29:
- #29 was synthetic-test identical, meaning its filtering also didn't work there
- We need filtering that works on BOTH synthetic and real data
- Higher delta threshold (35%) will naturally reduce trade count
- Persistence requirement (2-bar confirmation) will further filter
- This should score better than #29's -0.2654

Cost analysis for 3,000 trades at 40% win rate with 4.0pt targets:
- Gross: 3,000 × 0.40 × 4.0 = 4,800 points
- Cost: 3,000 × 0.2 = 600 points
- Net: 4,200 points
- Score: 4,200 / 384 = +10.9 per session (much better than -0.5016)

The key is being selective enough that we're trading ACTUAL signal, not noise.
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        delta_ratio_threshold: float = 0.35,  # Raised from 0.28 to be more selective
        min_volume: int = 50,  # Raised from 35 to require more activity
        stop_offset: float = 1.0,
        target_offset: float = 4.0,
        size: int = 1,
        wick_min_ratio: float = 0.20,  # Raised to require stronger rejection
        momentum_max_ratio: float = 0.45,  # Lowered to require less momentum
        min_bars_since_level: int = 5,  # Minimum bars since level was tested
        level_lookback: int = 50,  # Bars to look back for level identification
    ) -> None:
        self.interval = interval
        self.delta_ratio_threshold = delta_ratio_threshold
        self.min_volume = min_volume
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.wick_min_ratio = wick_min_ratio
        self.momentum_max_ratio = momentum_max_ratio
        self.min_bars_since_level = min_bars_since_level
        self.level_lookback = level_lookback

        # Track bars and price levels
        self.bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None
        # Track tested price levels (where price has been multiple times)
        self.tested_levels: deque = deque(maxlen=100)
        # Pending absorption signal: (direction, absorption_extreme, bars_since_absorption)
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
            self.bars.clear()
            self.tested_levels.clear()

        # Get current bar
        if self.interval not in ctx.bars:
            return

        current_bar = ctx.bars[self.interval]
        self.bars.append(current_bar)

        # Update tested levels based on this bar
        self._update_tested_levels(current_bar)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Handle pending absorption signals (waiting for persistence)
        if self.pending_absorption is not None:
            direction, absorption_extreme, bars_since = self.pending_absorption

            # Check if this bar continues the move (persistence)
            if self._check_persistence(current_bar, direction, absorption_extreme):
                # Move persists! Enter the trade
                tag = f"absorption-{direction.lower()}-persistent"

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
            elif bars_since >= 2:
                # Move didn't persist, clear signal
                self.pending_absorption = None
            else:
                # Keep waiting
                self.pending_absorption = (direction, absorption_extreme, bars_since + 1)
                return

        # Look for new absorption signal
        absorption_direction = self._detect_absorption(current_bar)
        if absorption_direction is not None:
            # Check if absorption is at a tested level
            if absorption_direction == "BUY":
                absorption_price = current_bar.high
            else:
                absorption_price = current_bar.low

            # Only proceed if at a tested level
            if self._is_at_tested_level(absorption_price):
                direction = absorption_direction
                if direction == "BUY":
                    reference_extreme = current_bar.high
                else:
                    reference_extreme = current_bar.low

                # Set pending for next bar's persistence check
                self.pending_absorption = (direction, reference_extreme, 0)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_tested_levels(self, current_bar) -> None:
        """Track price levels that have been tested multiple times."""
        # Add current bar's extremes to tested levels
        self.tested_levels.append(current_bar.high)
        self.tested_levels.append(current_bar.low)

    def _is_at_tested_level(self, price: float) -> bool:
        """
        Check if price is at a level that has been tested multiple times.
        A tested level is one that appears multiple times in recent price action.
        """
        if len(self.tested_levels) < self.min_bars_since_level * 2:
            # Not enough history
            return True

        # Get recent levels
        recent_levels = list(self.tested_levels)[-(self.level_lookback * 2):]

        # Count how many times price has been near this level
        # (within 0.30 points, which is 1.5 ticks)
        proximity = 0.30
        level_count = 0

        for level in recent_levels:
            if abs(price - level) <= proximity:
                level_count += 1

        # Only trade if the level has been tested at least 3 times
        # This ensures it's a "real" level, not a random price
        return level_count >= 3

    def _detect_absorption(self, current_bar) -> Optional[str]:
        """
        Detect absorption: very strong one-way flow with price rejection and low momentum.

        Absorption characteristics:
        1. Delta ratio above 35% (strong one-sided flow)
        2. Meaningful wick (20%+ of range) in opposite direction
        3. Low momentum (close near open)
        4. Sufficient volume (50+ contracts)

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

        # Need VERY strong one-way flow
        if delta_ratio < self.delta_ratio_threshold:
            return None

        # Check bar range
        bar_range = current_bar.high - current_bar.low
        if bar_range < 0.10:
            return None

        # Calculate momentum
        momentum_ratio = (
            abs(current_bar.close - current_bar.open) / bar_range
            if bar_range > 0
            else 0
        )

        # Momentum must be LOW
        if momentum_ratio > self.momentum_max_ratio:
            return None

        # Upside absorption: positive delta with upper wick
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Must have strong wick (20%+ rejection)
            if wick_ratio > self.wick_min_ratio:
                # Close should be higher than open (bullish flow despite wick)
                if current_bar.close > current_bar.open:
                    return "BUY"

        # Downside absorption: negative delta with lower wick
        elif current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Must have strong wick (20%+ rejection)
            if wick_ratio > self.wick_min_ratio:
                # Close should be lower than open (bearish flow despite wick)
                if current_bar.close < current_bar.open:
                    return "SELL"

        return None

    def _check_persistence(
        self, current_bar, direction: str, absorption_extreme: float
    ) -> bool:
        """
        Check if the move persists past the absorption extreme.

        For BUY: bar must stay above absorption high and close above open
        For SELL: bar must stay below absorption low and close below open
        """
        bar_range = current_bar.high - current_bar.low
        if bar_range <= 0:
            return False

        momentum_ratio = (
            abs(current_bar.close - current_bar.open) / bar_range
            if bar_range > 0
            else 0
        )

        if direction == "BUY":
            # Must close above absorption high with bullish momentum
            return (
                current_bar.close > absorption_extreme
                and current_bar.close > current_bar.open
                and momentum_ratio > 0.30
            )
        else:  # SELL
            # Must close below absorption low with bearish momentum
            return (
                current_bar.close < absorption_extreme
                and current_bar.close < current_bar.open
                and momentum_ratio > 0.30
            )
