"""
Volume profile absorption strategy - focus on volume-weighted price levels and flow analysis.

Learning from prior trials:
- #15 (best): score=-0.9148, 103,467 trades - high frequency, costs dominate
- Previous attempts all failed due to insufficient exposure or excessive trading
- The synthetic test shows we're generating trades, but they're not profitable

Key insight from campaign brief and data:
1. Must trade MOVES BETWEEN LEVELS, not scalp intra-bar
2. Four prior order-flow attempts failed due to COST (0.2 points per round trip)
3. Need 70%+ session exposure (270+ of 384 sessions) - multi-session holds help here
4. The absorption hypothesis: flow that fails to move price precedes moves AWAY from that level

New approach - back to fundamentals:
1. Build simple volume profile from recent bars (VPOC, value area edges)
2. Detect when price arrives at a profile level with strong one-way flow (absorption signal)
3. Trade directionally ONLY when absorption is present at a level
4. Use large targets (3-4 points) to survive costs - need 2+ points profit per trade
5. Structural stops at absorption bar extreme + small buffer
6. Generate fewer, higher-conviction trades
7. Tag trades to distinguish absorption-driven vs directional

The key difference: we're NOT trading every momentum bar. We're trading ONLY when:
- Price arrives at a volume level (from current or recent sessions)
- Strong one-way flow arrives with it (absorption)
- Setup is clean enough to hold for a multi-bar move

This should reduce trade frequency dramatically while maintaining exposure through longer holds.
"""

from __future__ import annotations
from collections import deque
from typing import Optional, Dict, Tuple


class Strategy:
    def __init__(
        self,
        slow_interval: str = "5m",
        profile_sessions: int = 2,
        lookback_bars: int = 5,
        delta_ratio_threshold: float = 0.30,
        volume_spike_ratio: float = 1.3,
        stop_offset: float = 0.75,
        target_offset: float = 3.5,
        size: int = 1,
        min_volume: int = 30,
    ) -> None:
        self.slow_interval = slow_interval
        self.profile_sessions = profile_sessions
        self.lookback_bars = lookback_bars
        self.delta_ratio_threshold = delta_ratio_threshold
        self.volume_spike_ratio = volume_spike_ratio
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume

        # Track bars for profile building and momentum detection
        self.slow_bars: deque = deque(maxlen=500)
        self.session_bars: Dict[int, list] = {}
        self.current_session: Optional[int] = None
        self.last_trade_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            if ctx.session not in self.session_bars:
                self.session_bars[ctx.session] = []

        # Get the current bar
        if self.slow_interval not in ctx.bars:
            return

        current_bar = ctx.bars[self.slow_interval]
        self.slow_bars.append(current_bar)
        self.session_bars[ctx.session].append(current_bar)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Skip if we just traded in this session (limit frequency)
        if self.last_trade_session == ctx.session:
            return

        # Need minimum history for analysis
        if len(self.slow_bars) < self.lookback_bars + 1:
            return

        # Get lookback window (prior N bars, not including current)
        window = list(self.slow_bars)[-(self.lookback_bars + 1) : -1]

        # Calculate baseline statistics from lookback window
        window_volume = sum(b.volume for b in window)
        avg_volume = window_volume / len(window) if window else 0

        if avg_volume < self.min_volume:
            return

        # FILTER 1: Volume spike requirement - current bar must show elevated activity
        if current_bar.volume < avg_volume * self.volume_spike_ratio:
            return

        # FILTER 2: Delta ratio - need strong one-way flow (absorption signature)
        if current_bar.volume > 0:
            delta_ratio = abs(current_bar.delta) / current_bar.volume
        else:
            delta_ratio = 0

        if delta_ratio < self.delta_ratio_threshold:
            return

        # FILTER 3: Build volume profile from recent bars to detect levels
        profile_prices = self._build_price_profile(window)
        if not profile_prices:
            return

        # FILTER 4: Check if current price is near a profile level
        near_level = self._is_near_profile_level(current_bar.close, profile_prices)
        if not near_level:
            return

        # FILTER 5: Directional bias must match delta
        if current_bar.close > current_bar.open and current_bar.delta > 0:
            # Uptrend with buying absorption
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-long",
            )
            self.last_trade_session = ctx.session
            return

        elif current_bar.close < current_bar.open and current_bar.delta < 0:
            # Downtrend with selling absorption
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="absorption-short",
            )
            self.last_trade_session = ctx.session
            return

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _build_price_profile(self, bars: list) -> set:
        """
        Build a simple price profile from bars - identify price levels where
        volume concentrated. Returns set of price levels.
        """
        if not bars:
            return set()

        # Bucket prices into levels (0.25 point buckets for NQ)
        bucket_size = 0.25
        price_buckets: Dict[int, int] = {}

        for bar in bars:
            # Distribute bar volume across its range
            # Use OHLC points as key price levels
            for price in [bar.open, bar.high, bar.low, bar.close]:
                bucket = int(price / bucket_size)
                price_buckets[bucket] = price_buckets.get(bucket, 0) + bar.volume // 4

        if not price_buckets:
            return set()

        # Find top volume buckets (above median)
        volumes = list(price_buckets.values())
        median_volume = sorted(volumes)[len(volumes) // 2] if volumes else 0

        # Return price levels where volume was above median
        profile_levels = set()
        for bucket, vol in price_buckets.items():
            if vol > median_volume:
                profile_levels.add(bucket * bucket_size)

        return profile_levels

    def _is_near_profile_level(self, price: float, profile_levels: set, tolerance: float = 0.5) -> bool:
        """
        Check if current price is near any identified profile level.
        Tolerance allows for price to be slightly away from exact level.
        """
        if not profile_levels:
            return False

        for level in profile_levels:
            if abs(price - level) < tolerance:
                return True

        return False
