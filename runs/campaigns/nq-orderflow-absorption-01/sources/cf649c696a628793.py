"""
Volume profile absorption strategy - focus on genuine multi-session holds with cost-aware sizing.

Learning from trial history (best: #17 at -0.5016 with 11,356 trades):
The synthetic test shows BYTE-IDENTICAL trades across multiple parameter changes,
indicating the core momentum + delta logic is what actually fires, not the 
confirmation bar mechanics we keep tweaking.

Root cause of -0.5016 score:
- 11,356 trades × 0.2 points cost = 2,271 points total cost
- Estimated gross P&L: ~2,079 points (to get net -192)
- Average profit per trade: 0.183 points
- With 6.0 point targets, only capturing 3% of target

The real problem: We're generating too many trades with targets that are 
unreachable given the cost structure. We need FEWER trades with BETTER win rates.

Key insight from campaign brief:
- "Multi-session holds are REQUIRED to reach 50% floor (192 sessions)"
- Holding for moves between volume profile levels requires patience
- A rule that trades 100+ times per session cannot hold long enough

New strategy - drastically reduce frequency while improving signal quality:
1. Only trade MAJOR absorption (delta ratio > 0.45, not 0.20-0.30)
2. Require the absorption bar to have EXTREME wick rejection (>25% of range)
3. Wait for volume-confirmed breakout (next bar must break extreme with volume)
4. Use MUCH larger targets (8-10 points) for multi-session holds
5. Structural stops just beyond absorption bar (0.75 points)
6. This creates a 4:1 reward-to-risk ratio achievable with 25% win rate

Expected outcome:
- Reduce to 2,000-3,500 trades total (5-9 per session average)
- Much higher win rate due to stricter signal + volume confirmation
- 25% win rate: 3,000 × 0.25 × 9.0 = 6,750 gross
- Cost: 3,000 × 0.2 = 600
- Net: 6,150 points positive (vs. -192 currently)

The hypothesis: Quality over quantity. The absorption signal IS real, but we've
been drowning it in noise by trading every directional bar. Extreme selectivity
with genuine confirmation should surface the real edge.
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        delta_ratio_threshold: float = 0.45,
        min_volume: int = 80,
        stop_offset: float = 0.75,
        target_offset: float = 9.0,
        size: int = 1,
        wick_min_ratio: float = 0.25,
        max_momentum_ratio: float = 0.40,
        volume_ratio_threshold: float = 1.30,
        confirmation_momentum_ratio: float = 0.70,
    ) -> None:
        self.interval = interval
        self.delta_ratio_threshold = delta_ratio_threshold
        self.min_volume = min_volume
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.wick_min_ratio = wick_min_ratio
        self.max_momentum_ratio = max_momentum_ratio
        self.volume_ratio_threshold = volume_ratio_threshold
        self.confirmation_momentum_ratio = confirmation_momentum_ratio

        # Track bars and pending absorption signals
        self.bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None
        # (direction, absorption_high, absorption_low, absorption_volume)
        self.pending_absorption: Optional[Tuple[str, float, float, int]] = None
        self.bars_since_absorption: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            self.pending_absorption = None
            self.bars_since_absorption = 0

        # Get current bar
        if self.interval not in ctx.bars:
            return

        current_bar = ctx.bars[self.interval]
        self.bars.append(current_bar)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Need minimum history
        if len(self.bars) < 2:
            return

        # If we have a pending absorption, ONLY check for confirmation
        if self.pending_absorption is not None:
            self.bars_since_absorption += 1
            direction, abs_high, abs_low, abs_volume = self.pending_absorption

            # Check if this bar shows volume-confirmed breakout
            if self._check_breakout_confirmation(
                current_bar, direction, abs_high, abs_low, abs_volume
            ):
                # Confirmed! Execute the trade
                tag = f"absorption-{direction.lower()}-confirmed"

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
                self.bars_since_absorption = 0
            elif self.bars_since_absorption > 5:
                # Confirmation failed after 5 bars, give up on this absorption
                self.pending_absorption = None
                self.bars_since_absorption = 0
            # Otherwise keep waiting for confirmation
            return

        # No pending absorption, look for new absorption signal
        absorption_info = self._detect_absorption(current_bar)
        if absorption_info is not None:
            direction, abs_high, abs_low, abs_volume = absorption_info
            # Store as pending - will ONLY check for confirmation next bar
            self.pending_absorption = (direction, abs_high, abs_low, abs_volume)
            self.bars_since_absorption = 0
            return

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption(self, current_bar) -> Optional[Tuple[str, float, float, int]]:
        """
        Detect EXTREME absorption: very strong one-way flow with severe price rejection.

        Absorption characteristics:
        1. Very high delta ratio (45%+ one-sided) - not just directional, DOMINANT
        2. EXTREME wick in opposite direction (25%+ of range) - price forcefully rejected
        3. Very low momentum (close near open) - flow without follow-through
        4. Sufficient volume (80+ contracts)

        Returns (direction, high, low, volume) or None.
        """
        if current_bar.volume < self.min_volume:
            return None

        # Calculate delta ratio
        delta_ratio = (
            abs(current_bar.delta) / current_bar.volume
            if current_bar.volume > 0
            else 0
        )

        # Need VERY strong one-way flow (45%+)
        if delta_ratio < self.delta_ratio_threshold:
            return None

        # Check bar range
        bar_range = current_bar.high - current_bar.low
        if bar_range < 0.15:
            return None

        # Calculate momentum
        momentum_ratio = (
            abs(current_bar.close - current_bar.open) / bar_range
            if bar_range > 0
            else 0
        )

        # Momentum must be LOW
        if momentum_ratio > self.max_momentum_ratio:
            return None

        # Upside absorption: positive delta with EXTREME upper wick
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Must have EXTREME wick (25%+ rejection)
            if wick_ratio > self.wick_min_ratio and current_bar.close > current_bar.open:
                return (
                    "BUY",
                    current_bar.high,
                    current_bar.low,
                    current_bar.volume,
                )

        # Downside absorption: negative delta with EXTREME lower wick
        elif current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Must have EXTREME wick (25%+ rejection)
            if wick_ratio > self.wick_min_ratio and current_bar.close < current_bar.open:
                return (
                    "SELL",
                    current_bar.high,
                    current_bar.low,
                    current_bar.volume,
                )

        return None

    def _check_breakout_confirmation(
        self,
        current_bar,
        direction: str,
        abs_high: float,
        abs_low: float,
        abs_volume: int,
    ) -> bool:
        """
        Check if current bar shows a volume-confirmed breakout past the absorption extreme.

        For BUY: bar must close above absorption_high with strong volume and momentum
        For SELL: bar must close below absorption_low with strong volume and momentum
        """
        bar_range = current_bar.high - current_bar.low
        if bar_range <= 0:
            return False

        # Check volume confirmation (must be significantly elevated)
        if current_bar.volume < abs_volume * self.volume_ratio_threshold:
            return False

        # Check momentum in breakout direction
        momentum_ratio = (
            (current_bar.close - current_bar.open) / bar_range
            if bar_range > 0
            else 0
        )

        if direction == "BUY":
            # Must close above the absorption high (breakout)
            # AND show strong bullish momentum
            if (
                current_bar.close > abs_high
                and momentum_ratio > self.confirmation_momentum_ratio
            ):
                return True

        else:  # SELL
            # Must close below the absorption low (breakout)
            # AND show strong bearish momentum
            if (
                current_bar.close < abs_low
                and momentum_ratio < -self.confirmation_momentum_ratio
            ):
                return True

        return False
