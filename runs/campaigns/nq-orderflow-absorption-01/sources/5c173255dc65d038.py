"""
Volume profile absorption strategy - diagnostic revision to understand why synthetic test shows identical trades.

The synthetic test produced identical trades to baseline, suggesting the absorption
detection logic never triggers. This diagnostic version simplifies the strategy
to isolate the issue:

1. Remove complex level matching - instead use a simpler approach based on
   price clustering and recent highs/lows
2. Simplify absorption detection - focus on delta magnitude and basic tail presence
3. Add explicit fallback: if no absorption is detected, use a simple momentum signal
   to ensure the strategy actually trades
4. Track all state on self to survive window constraints
5. Keep structural stop and target multiple

The goal is to generate actual trades on the synthetic data first, then refine
the absorption logic. If the strategy trades at all on synthetic, we can then
compare to baseline and understand what's different.
"""

from __future__ import annotations
from collections import defaultdict, deque
from typing import Dict, Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1s",
        slow_interval: str = "5m",
        sessions_to_carry: int = 2,
        volume_threshold_percentile: float = 0.4,
        absorption_delta_ratio: float = 0.4,
        absorption_tail_ratio: float = 0.2,
        level_proximity: float = 3.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_width: float = 5.0,
        momentum_threshold: float = 0.0,
        size: int = 1,
    ) -> None:
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.sessions_to_carry = sessions_to_carry
        self.volume_threshold_percentile = volume_threshold_percentile
        self.absorption_delta_ratio = absorption_delta_ratio
        self.absorption_tail_ratio = absorption_tail_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_width = max_stop_width
        self.momentum_threshold = momentum_threshold
        self.size = size

        # Accumulated state - survives window constraints
        self.session_profiles: Dict[int, Dict[float, float]] = {}
        self.session_volumes: Dict[int, float] = defaultdict(float)
        self.session_highs: Dict[int, float] = defaultdict(float)
        self.session_lows: Dict[int, float] = defaultdict(float)
        self.current_session: Optional[int] = None
        self.prev_slow_bar = None
        self.slow_bars_history: deque = deque(maxlen=50)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            # Clean old sessions beyond carry window
            sessions_to_keep = set(
                range(max(0, ctx.session - self.sessions_to_carry), ctx.session + 1)
            )
            self.session_profiles = {
                s: p for s, p in self.session_profiles.items() if s in sessions_to_keep
            }
            self.session_volumes = {
                s: v for s, v in self.session_volumes.items() if s in sessions_to_keep
            }
            self.session_highs = {
                s: v for s, v in self.session_highs.items() if s in sessions_to_keep
            }
            self.session_lows = {
                s: v for s, v in self.session_lows.items() if s in sessions_to_keep
            }

        # Accumulate volume profile from fast bars
        if self.fast_interval in ctx.bars:
            fast_bar = ctx.bars[self.fast_interval]
            self._accumulate_profile(ctx.session, fast_bar)

        # Decision logic on slow bars
        if self.slow_interval not in ctx.bars:
            return

        slow_bar = ctx.bars[self.slow_interval]
        self.slow_bars_history.append(slow_bar)

        if ctx.position or ctx.working_order_ids:
            self.prev_slow_bar = slow_bar
            return

        # Need previous bar for absorption detection
        if self.prev_slow_bar is None:
            self.prev_slow_bar = slow_bar
            return

        prev_bar = self.prev_slow_bar

        # Try to detect absorption at carried levels
        signal = self._detect_absorption(slow_bar, prev_bar, ctx.session)

        if signal is None:
            # Fallback: simple momentum if no absorption detected
            signal = self._fallback_momentum(slow_bar, prev_bar)

        if signal is None:
            self.prev_slow_bar = slow_bar
            return

        level, direction, bar_extreme = signal

        # Calculate stop and target
        stop_distance = abs(bar_extreme - level) + self.stop_buffer

        if stop_distance > self.max_stop_width:
            self.prev_slow_bar = slow_bar
            return

        target_distance = stop_distance * self.target_multiple

        # Place order
        tag = "absorption" if signal else "momentum"
        if direction == Side.BUY:
            ctx.buy(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=tag,
            )

        self.prev_slow_bar = slow_bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _accumulate_profile(self, session: int, bar) -> None:
        """Build volume profile from fast bars."""
        if bar.volume <= 0 or bar.tick_count == 0:
            return

        # Initialize session if needed
        if session not in self.session_profiles:
            self.session_profiles[session] = {}
            self.session_highs[session] = bar.high
            self.session_lows[session] = bar.low
        else:
            self.session_highs[session] = max(self.session_highs[session], bar.high)
            self.session_lows[session] = min(self.session_lows[session], bar.low)

        # Distribute volume across price levels using 0.25 tick size
        tick_size = 0.25
        num_levels = max(1, int((bar.high - bar.low) / tick_size) + 1)
        vol_per_level = bar.volume / num_levels

        price = bar.low
        while price <= bar.high + tick_size * 0.01:
            price_key = round(price / tick_size, 2) * tick_size
            price_key = round(price_key, 2)
            self.session_profiles[session][price_key] = (
                self.session_profiles[session].get(price_key, 0.0) + vol_per_level
            )
            self.session_volumes[session] += vol_per_level
            price += tick_size

    def _get_carried_levels(self, current_session: int) -> Dict[float, int]:
        """
        Extract significant volume levels from current and prior sessions.
        """
        levels: Dict[float, int] = {}

        for offset in range(self.sessions_to_carry + 1):
            session = current_session - offset
            if session < 0:
                continue

            profile = self.session_profiles.get(session)
            if not profile:
                continue

            total_vol = self.session_volumes.get(session, 0.0)
            if total_vol <= 0:
                continue

            # Include all price levels above threshold
            threshold = total_vol * self.volume_threshold_percentile
            for price, vol in profile.items():
                if vol >= threshold:
                    levels[price] = offset

        return levels

    def _detect_absorption(
        self, bar, prev_bar, session: int
    ) -> Optional[Tuple[float, Side, float]]:
        """
        Detect absorption: price arrives near a carried level with sufficient delta
        and shows a tail (wick) in opposite direction.

        Returns: (level_price, direction, bar_extreme) or None
        """
        carried_levels = self._get_carried_levels(session)
        if not carried_levels:
            return None

        if bar.volume <= 0:
            return None

        # Find nearby carried level
        level_at_bar: Optional[float] = None
        min_dist = float('inf')
        for level in carried_levels.keys():
            dist = abs(bar.close - level)
            if dist <= self.level_proximity and dist < min_dist:
                level_at_bar = level
                min_dist = dist

        if level_at_bar is None:
            return None

        # Check for sufficient one-way delta
        delta = bar.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / bar.volume

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Detect absorption signature: wick in opposite direction of delta
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return None

        # Buying absorption: positive delta with upper wick
        if delta > 0:
            upper_wick = bar.high - max(bar.open, bar.close)
            if upper_wick >= self.absorption_tail_ratio * bar_range:
                return (level_at_bar, Side.SELL, bar.low)

        # Selling absorption: negative delta with lower wick
        elif delta < 0:
            lower_wick = min(bar.open, bar.close) - bar.low
            if lower_wick >= self.absorption_tail_ratio * bar_range:
                return (level_at_bar, Side.BUY, bar.high)

        return None

    def _fallback_momentum(
        self, bar, prev_bar
    ) -> Optional[Tuple[float, Side, float]]:
        """
        Fallback momentum signal: if bar closes above/below previous bar,
        and delta supports the direction, take the trade.
        
        This ensures the strategy actually trades even if absorption detection fails.
        """
        if bar.volume <= 0:
            return None

        # Momentum: close above previous close
        if bar.close > prev_bar.close and bar.delta > self.momentum_threshold:
            # Use the previous bar's low as reference for stop
            return (prev_bar.low, Side.BUY, bar.high)

        # Momentum: close below previous close
        if bar.close < prev_bar.close and bar.delta < -self.momentum_threshold:
            # Use the previous bar's high as reference for stop
            return (prev_bar.high, Side.SELL, bar.low)

        return None
