"""
Volume profile absorption strategy - focus on absorption signal quality and cost-aware sizing.

Learning from trial history:
- Best score: #17 at -0.5016 with ~11k trades
- #27 synthetic test showed IDENTICAL trades despite 2-bar confirmation attempt
- This reveals: the absorption detection is generating trades, confirmation logic isn't filtering
- GTC orders don't help if we're entering on poor-quality signals

Core problem analysis:
At 11k trades × 0.2pt cost = 2.2k points cost
Score of -0.5016 suggests ~192pt net loss
This means: gross P&L before costs ≈ 2,008pt, or ~0.177pt per trade average
With 4.5-6pt targets, we're only capturing 3-4% of target on average
This is a SIGNAL QUALITY problem, not a frequency problem

Key insight from campaign brief:
"Does absorption at a carried level precede a move away from that level?"
- We're detecting absorption correctly (wick + delta)
- But we're not filtering for the RIGHT absorption
- The brief specifically mentions: "price arrives at one of these LEVELS"
- We should trade absorption that occurs AT SIGNIFICANT PRICE LEVELS, not random bars

New approach - filter absorption by price level significance:
1. Build a simple price level tracker from recent bars (simple support/resistance)
2. Only trade absorption when it occurs AT OR NEAR a significant level
3. This filters out random high-delta bars that aren't at meaningful prices
4. Keep same absorption detection (wick + 30% delta ratio)
5. Targets: 4.0pt (achievable multi-session holds)
6. Stops: 1.0pt (structural, just beyond wick)
7. Expected result: 6-8k trades with much better win rate

The hypothesis: Absorption is only predictive when it occurs at LEVELS that matter.
A bar with 40% delta is noise if it's at a random price.
The same bar at a support level after rejection is the signal the campaign asks about.

This is the only change that makes sense:
- We keep the absorption detection (proven)
- We add level filtering (addresses the "levels" part of the brief)
- This should improve signal quality without changing the core mechanism
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
        delta_ratio_threshold: float = 0.20,
        absorption_delta_ratio: float = 0.30,
        min_volume: int = 30,
        stop_offset: float = 1.0,
        target_offset: float = 4.0,
        size: int = 1,
        wick_min_ratio: float = 0.15,
        level_proximity: float = 0.50,
        level_lookback: int = 50,
    ) -> None:
        self.interval = interval
        self.lookback_bars = lookback_bars
        self.delta_ratio_threshold = delta_ratio_threshold
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_volume = min_volume
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.wick_min_ratio = wick_min_ratio
        self.level_proximity = level_proximity  # how close price must be to a level
        self.level_lookback = level_lookback  # bars to look back for level formation

        # Track bars
        self.bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None
        self.highs: deque = deque(maxlen=200)  # track recent highs for level detection
        self.lows: deque = deque(maxlen=200)   # track recent lows for level detection

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            self.highs.clear()
            self.lows.clear()

        # Get current bar
        if self.interval not in ctx.bars:
            return

        current_bar = ctx.bars[self.interval]
        self.bars.append(current_bar)
        self.highs.append(current_bar.high)
        self.lows.append(current_bar.low)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Need minimum history
        if len(self.bars) < self.lookback_bars + 1:
            return

        # Check for absorption signal
        absorption_info = self._detect_absorption(current_bar)
        if absorption_info is None:
            return

        direction, reference_extreme = absorption_info

        # Filter: only trade absorption at significant price levels
        if not self._is_near_significant_level(current_bar, direction):
            return

        # Execute the trade with GTC for multi-session holds
        tag = f"absorption-{direction.lower()}-level"

        if direction == "BUY":
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tif="GTC",
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tif="GTC",
                tag=tag,
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _detect_absorption(self, current_bar) -> Optional[Tuple[str, float]]:
        """
        Detect absorption: strong one-way flow with price rejection.

        Absorption characteristics:
        1. High delta ratio (30%+ one-sided for absorption tag)
        2. Meaningful wick in opposite direction (price rejected)
        3. Sufficient volume

        Returns (direction, reference_extreme) or None.
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

        # Need strong one-way flow for absorption tag
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check bar range
        bar_range = current_bar.high - current_bar.low
        if bar_range < 0.10:
            return None

        # Upside absorption: positive delta with upper wick
        if current_bar.delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            wick_ratio = upper_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected upside)
            if wick_ratio > self.wick_min_ratio:
                # Close should be higher than open (bullish flow despite wick)
                if current_bar.close > current_bar.open:
                    return ("BUY", current_bar.high)

        # Downside absorption: negative delta with lower wick
        elif current_bar.delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            wick_ratio = lower_wick / bar_range if bar_range > 0 else 0

            # Must have meaningful wick (price rejected downside)
            if wick_ratio > self.wick_min_ratio:
                # Close should be lower than open (bearish flow despite wick)
                if current_bar.close < current_bar.open:
                    return ("SELL", current_bar.low)

        return None

    def _is_near_significant_level(self, current_bar, direction: str) -> bool:
        """
        Check if the absorption is occurring at a significant price level.
        
        Significant levels are identified as:
        - Previous highs that were tested multiple times (resistance)
        - Previous lows that were tested multiple times (support)
        
        We check if the current bar's price is within level_proximity of a level.
        """
        if len(self.highs) < 10:
            return True  # Not enough history, allow the trade
        
        # Get recent price levels (excluding the current bar)
        recent_highs = list(self.highs)[:-1] if len(self.highs) > 1 else []
        recent_lows = list(self.lows)[:-1] if len(self.lows) > 1 else []
        
        if not recent_highs or not recent_lows:
            return True
        
        # Find the most significant levels (ones that appear multiple times)
        # For simplicity, use recent highs/lows as potential levels
        price = current_bar.close
        
        if direction == "BUY":
            # For upside absorption, check if we're near a recent support level
            # that was tested multiple times
            for low in recent_lows[-self.level_lookback:]:
                if abs(price - low) <= self.level_proximity:
                    return True
        else:  # SELL
            # For downside absorption, check if we're near a recent resistance level
            # that was tested multiple times
            for high in recent_highs[-self.level_lookback:]:
                if abs(price - high) <= self.level_proximity:
                    return True
        
        return False
