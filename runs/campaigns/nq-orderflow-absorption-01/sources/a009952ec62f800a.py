"""
Volume profile absorption strategy - focus on frequency-adjusted entry with multi-bar holds.

Analysis of trial progression:
- #17 (best): score=-0.5016, 11,356 trades (~29/session) - OPTIMAL
- #15: score=-0.9148, 103,467 trades (~270/session) - costs kill high frequency
- #16: score=-0.8355, 27,665 trades (~72/session)
- #14: score=-0.9699, 56,004 trades (~146/session)

The winning pattern is clear: Lower frequency = Better score.
At ~29 trades/session we get -0.50. At ~270 trades/session we get -0.91.

Current synthetic test shows 70 trades on small dataset - proportionally too high.
If we're generating 70 trades on synthetic warmup, we're likely generating 200+ per session on real data.

The problem: Absorption filters are not tight enough. We're still trading directional bars
that lack true absorption characteristics. The synthetic test being IDENTICAL to current
means our parameter changes haven't actually changed the entry logic meaningfully.

Root cause analysis:
- Current thresholds (delta_ratio > 0.30 for entry, > 0.45 for absorption) are still too loose
- The absorption check requires wick_ratio > 0.25 BUT wick is calculated wrong
- We're trading bars that show directional bias but not actual price rejection
- Need to be 5-10x more selective to get from 70 trades down to 7-10 trades on synthetic

New approach - DRASTICALLY reduce frequency:
1. Increase min_delta_ratio from 0.30 to 0.60 (only extreme one-way flow)
2. Increase absorption_delta_ratio from 0.45 to 0.65 (must be overwhelming)
3. Require BOTH strong delta AND strong wick (not OR)
4. Wick must be >30% of range (was 25%, need stricter)
5. Body must be >40% of range (was 30%, need more commitment)
6. Only trade when prior bar also showed directional bias (confirmation)
7. This should reduce frequency by 10x: from 70 to 7 trades on synthetic

Keep the winning stop/target from #17: 1.25 / 6.0
These targets work because with much lower frequency, each trade has more time to develop.
Multi-session holds naturally occur with larger targets.
"""

from __future__ import annotations
from collections import deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        bar_interval: str = "5m",
        lookback_bars: int = 3,
        min_delta_ratio: float = 0.60,
        absorption_delta_ratio: float = 0.65,
        stop_offset: float = 1.25,
        target_offset: float = 6.0,
        size: int = 1,
        min_volume: int = 80,
        wick_ratio_threshold: float = 0.30,
        min_body_ratio: float = 0.40,
    ) -> None:
        self.bar_interval = bar_interval
        self.lookback_bars = lookback_bars
        self.min_delta_ratio = min_delta_ratio
        self.absorption_delta_ratio = absorption_delta_ratio
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.wick_ratio_threshold = wick_ratio_threshold
        self.min_body_ratio = min_body_ratio

        # Track bars for analysis
        self.bars: deque = deque(maxlen=512)
        self.current_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.bar_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session
        if ctx.session != self.current_session:
            self.current_session = ctx.session

        # Get current bar
        if self.bar_interval not in ctx.bars:
            return

        current_bar = ctx.bars[self.bar_interval]
        self.bars.append(current_bar)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Need minimum history
        if len(self.bars) < self.lookback_bars + 1:
            return

        # Minimum volume filter
        if current_bar.volume < self.min_volume:
            return

        # Calculate delta ratio
        delta_ratio = (
            abs(current_bar.delta) / current_bar.volume
            if current_bar.volume > 0
            else 0
        )

        # Need EXTREME delta to consider (0.60+ = 60%+ one-sided)
        if delta_ratio < self.min_delta_ratio:
            return

        # Get prior bar for comparison
        prior_bar = self.bars[-2]

        # Check for absorption characteristics - VERY STRICT filter
        absorption_signal = self._check_absorption_extreme(current_bar, prior_bar, delta_ratio)
        
        if absorption_signal is None:
            return

        direction = absorption_signal

        # Trade based on absorption signal
        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-long",
            )
            return

        elif direction == "short":
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-short",
            )
            return

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _check_absorption_extreme(
        self, current_bar, prior_bar, delta_ratio: float
    ) -> Optional[str]:
        """
        EXTREME absorption detection - only trade the clearest cases:
        1. OVERWHELMING one-way delta (65%+ of volume)
        2. STRONG wick formation (30%+ of range) showing clear price rejection
        3. STRONG body (40%+ of range) showing commitment and follow-through
        4. Direction must match: positive delta with close > open, or negative delta with close < open
        5. Prior bar must also show directional bias (confirmation)
        6. Bar must not extend too far past prior bar (absorption, not breakout)
        
        Returns "long" for upside absorption, "short" for downside absorption, or None.
        """
        bar_range = current_bar.high - current_bar.low
        
        # Need meaningful range to detect wicks and body
        if bar_range < 0.20:
            return None

        # Need OVERWHELMING delta for absorption (65%+ threshold)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Calculate body and wick sizes
        body = abs(current_bar.close - current_bar.open)
        body_ratio = body / bar_range if bar_range > 0 else 0

        # Need STRONG body (not just wicks)
        if body_ratio < self.min_body_ratio:
            return None

        # Check prior bar for directional confirmation
        prior_delta_ratio = (
            abs(prior_bar.delta) / prior_bar.volume
            if prior_bar.volume > 0
            else 0
        )
        
        # Prior bar must also show strong directional bias (0.40+)
        if prior_delta_ratio < 0.40:
            return None

        # Upside absorption: positive delta with close > open (buying pressure confirmed)
        if current_bar.delta > 0 and current_bar.close > current_bar.open:
            # Prior bar must also show upside bias
            if prior_bar.delta <= 0:
                return None
                
            # Upper wick is distance from high to close
            upper_wick = current_bar.high - current_bar.close
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0
            
            # Must have STRONG wick showing price rejection at higher levels
            if wick_ratio >= self.wick_ratio_threshold:
                # Verify bar didn't extend far above prior bar (absorption, not breakout)
                if current_bar.high <= prior_bar.high + 1.0:
                    return "long"

        # Downside absorption: negative delta with close < open (selling pressure confirmed)
        elif current_bar.delta < 0 and current_bar.close < current_bar.open:
            # Prior bar must also show downside bias
            if prior_bar.delta >= 0:
                return None
                
            # Lower wick is distance from low to close
            lower_wick = current_bar.close - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0
            
            # Must have STRONG wick showing price rejection at lower levels
            if wick_ratio >= self.wick_ratio_threshold:
                # Verify bar didn't extend far below prior bar (absorption, not breakdown)
                if current_bar.low >= prior_bar.low - 1.0:
                    return "short"

        return None
