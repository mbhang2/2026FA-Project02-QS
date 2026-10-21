"""
Volume profile absorption strategy - testing directional bias with cost-aware sizing.

Analysis of prior attempts:
- #15 scored -0.9148 with 103,467 trades (high frequency, losing money)
- #14 scored -0.9699 with 56,004 trades (lower frequency, worse)
- Current strategy trades on every 5m bar where close > open + positive delta OR close < open + negative delta
- This generates ~268 trades per session, which is essentially scalping at 0.2 point cost per round trip

The synthetic test showed IDENTICAL fills to current strategy, which means:
1. The current signal (directional + delta alignment) fires frequently enough
2. The problem is not the signal frequency - it's the cost model and target size
3. With 0.2 point round-trip cost and 4.5 point targets, we need only ~4.4% win rate to break even
4. But we're losing money, so the signal itself must have negative expectancy

Key insight from campaign brief:
1. "Does absorption at a carried level precede a move away?" - this requires LEVEL detection
2. We've been trading directional momentum, not absorption at levels
3. The campaign asks for volume profile levels, not just directional bias
4. We need to actually build and use volume profile data

New approach:
1. Build a simple volume profile from recent bars (VPOC-like tracking)
2. Identify price levels where significant volume traded
3. Trade directionally ONLY when price is near a carried level AND shows directional signal
4. This adds a structural filter: "is price at a level?" before we trade
5. Smaller target (2.5 points) but much higher conviction - we're trading at defined levels
6. Keep the same frequency floor by trading at multiple levels across sessions

This directly addresses the campaign question: we're now checking if price is at a
volume-defined level before entering, which is what "absorption at a carried level"
requires. The directional signal (delta + close direction) acts as confirmation.
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Dict, List, Tuple


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        stop_offset: float = 0.5,
        target_offset: float = 2.5,
        min_volume: int = 30,
        min_delta_ratio: float = 0.10,
        size: int = 1,
        level_lookback: int = 20,
        price_level_tolerance: float = 0.5,
    ) -> None:
        self.interval = interval
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.min_volume = min_volume
        self.min_delta_ratio = min_delta_ratio
        self.size = size
        self.level_lookback = level_lookback
        self.price_level_tolerance = price_level_tolerance

        # Track bars for volume profile
        self.recent_bars: deque = deque(maxlen=500)
        self.current_session: Optional[int] = None
        self.volume_at_price: Dict[float, int] = {}

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def _get_price_levels(self) -> List[float]:
        """
        Identify significant price levels from recent volume.
        Returns price levels where significant volume traded.
        """
        if not self.recent_bars or len(self.recent_bars) < 5:
            return []

        # Build volume profile from recent bars
        price_volume: Dict[float, int] = {}
        
        for bar in list(self.recent_bars)[-self.level_lookback:]:
            # Discretize price to nearest 0.5 point for level identification
            price_level = round(bar.high / self.price_level_tolerance) * self.price_level_tolerance
            if price_level not in price_volume:
                price_volume[price_level] = 0
            price_volume[price_level] += bar.volume

            price_level = round(bar.low / self.price_level_tolerance) * self.price_level_tolerance
            if price_level not in price_volume:
                price_volume[price_level] = 0
            price_volume[price_level] += bar.volume

            price_level = round(bar.close / self.price_level_tolerance) * self.price_level_tolerance
            if price_level not in price_volume:
                price_volume[price_level] = 0
            price_volume[price_level] += bar.volume

        # Return levels with above-average volume
        if not price_volume:
            return []
        
        avg_volume = sum(price_volume.values()) / len(price_volume)
        significant_levels = [p for p, v in price_volume.items() if v > avg_volume * 0.8]
        
        return sorted(significant_levels)

    def _is_near_level(self, price: float, levels: List[float]) -> bool:
        """Check if current price is near a significant volume level."""
        if not levels:
            return False
        
        for level in levels:
            if abs(price - level) <= self.price_level_tolerance:
                return True
        return False

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            self.volume_at_price.clear()

        # Get the current bar
        if self.interval not in ctx.bars:
            return

        current_bar = ctx.bars[self.interval]
        self.recent_bars.append(current_bar)

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Volume filter - need meaningful volume
        if current_bar.volume < self.min_volume:
            return

        # Delta ratio filter - require directional flow
        delta_ratio = abs(current_bar.delta) / current_bar.volume if current_bar.volume > 0 else 0
        if delta_ratio < self.min_delta_ratio:
            return

        # Get significant price levels from recent history
        levels = self._get_price_levels()

        # Buy signal: close > open AND positive delta AND price near a level
        if current_bar.close > current_bar.open and current_bar.delta > 0:
            if self._is_near_level(current_bar.close, levels) or self._is_near_level(current_bar.high, levels):
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="level-absorption-long",
                )
                return

        # Sell signal: close < open AND negative delta AND price near a level
        if current_bar.close < current_bar.open and current_bar.delta < 0:
            if self._is_near_level(current_bar.close, levels) or self._is_near_level(current_bar.low, levels):
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="level-absorption-short",
                )
                return

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
