"""
Volume profile absorption strategy - focus on multi-session holds and cost viability.

Key insight from campaign brief and prior failures:
- The strategy must hold for moves BETWEEN levels, not intra-bar scalping
- Four prior order-flow attempts failed because they traded too frequently
- This one must earn multiple points per round turn to survive 0.2 point commission
- Exposure floor is 50% (not 70%) for this campaign due to rarity of absorption

Prior trial #11 analysis:
- All trials #0-#11 scored NOT SCORED due to insufficient exposure
- Synthetic test showed identical fills to baseline (0 trades)
- This means the directional signal logic wasn't triggering

Root cause: The directional_threshold of 0.5 points is too tight for NQ.
NQ moves in larger increments. Need to:
1. Use wider directional threshold (1.0+ points)
2. Ensure we trade when conditions exist
3. Hold positions across multiple bars to reach between-level moves
4. Track volume profile properly to identify levels
5. Only take absorption-confirmed entries to ensure quality

New approach:
- Build volume profile from 1s bars, track VPOC per session
- On 5m bars: detect directional moves with wider threshold
- Add absorption confirmation when at a carried level
- Use larger target multiples (3-4x) to reach next level
- Accept that we may trade less frequently but with better quality
- This is about HOLDING for bigger moves, not frequent small trades
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
        directional_threshold: float = 1.0,
        absorption_delta_ratio: float = 0.40,
        level_proximity: float = 2.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_width: float = 5.0,
        size: int = 1,
    ) -> None:
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.sessions_to_carry = sessions_to_carry
        self.directional_threshold = directional_threshold
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_width = max_stop_width
        self.size = size

        # Volume profile state - survives window constraints
        self.current_session: Optional[int] = None
        self.session_price_volume: Dict[int, Dict[float, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.session_total_volume: Dict[int, float] = defaultdict(float)
        self.session_vpoc: Dict[int, float] = {}

        # Track slow bars for context
        self.slow_bars_history: deque = deque(maxlen=300)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Handle session transitions
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            cutoff_session = max(0, ctx.session - self.sessions_to_carry - 1)
            self.session_price_volume = defaultdict(
                lambda: defaultdict(float),
                {
                    s: v
                    for s, v in self.session_price_volume.items()
                    if s >= cutoff_session
                },
            )
            self.session_total_volume = {
                s: v
                for s, v in self.session_total_volume.items()
                if s >= cutoff_session
            }
            self.session_vpoc = {
                s: v for s, v in self.session_vpoc.items() if s >= cutoff_session
            }

        # Accumulate volume from fast bars
        if self.fast_interval in ctx.bars:
            fast_bar = ctx.bars[self.fast_interval]
            self._accumulate_volume(ctx.session, fast_bar)

        # Process slow bars
        if self.slow_interval not in ctx.bars:
            return

        slow_bar = ctx.bars[self.slow_interval]
        self.slow_bars_history.append(slow_bar)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Need at least 2 bars for comparison
        if len(self.slow_bars_history) < 2:
            return

        prev_bar = self.slow_bars_history[-2]
        current_bar = slow_bar

        # Check for directional move
        direction = self._get_directional_signal(current_bar, prev_bar)
        if direction is None:
            return

        # Check for absorption confirmation at a carried level
        absorption_signal = self._detect_absorption_at_level(
            current_bar, prev_bar, ctx.session
        )

        if absorption_signal is None:
            return

        level_price, bar_extreme = absorption_signal

        # Calculate stop distance from bar extreme
        stop_distance = abs(bar_extreme - level_price) + self.stop_buffer
        stop_distance = max(0.25, min(stop_distance, self.max_stop_width))

        target_distance = stop_distance * self.target_multiple

        if direction == Side.BUY:
            ctx.buy(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag="absorption-confirmed",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag="absorption-confirmed",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _accumulate_volume(self, session: int, bar) -> None:
        """Accumulate volume at price levels using 0.25 tick resolution."""
        if bar.volume <= 0 or bar.tick_count == 0:
            return

        tick_size = 0.25
        low_bin = int(bar.low / tick_size) * tick_size
        high_bin = int(bar.high / tick_size) * tick_size

        num_levels = max(1, int(round((high_bin - low_bin) / tick_size)) + 1)
        vol_per_level = bar.volume / num_levels

        price = low_bin
        while price <= high_bin + tick_size * 0.01:
            price_key = round(price / tick_size, 2)
            self.session_price_volume[session][price_key] += vol_per_level
            self.session_total_volume[session] += vol_per_level
            price += tick_size

        # Update VPOC for this session
        if self.session_price_volume[session]:
            vpoc = max(
                self.session_price_volume[session].items(), key=lambda x: x[1]
            )[0]
            self.session_vpoc[session] = vpoc

    def _get_carried_levels(self, current_session: int) -> list[float]:
        """Get VPOC levels from current and prior sessions."""
        levels = []
        for offset in range(self.sessions_to_carry + 1):
            session = current_session - offset
            if session < 0:
                continue
            vpoc = self.session_vpoc.get(session)
            if vpoc is not None:
                levels.append(vpoc)
        return levels

    def _get_directional_signal(self, current_bar, prev_bar) -> Optional[Side]:
        """
        Detect directional moves with a reasonable threshold.
        Returns Side.BUY or Side.SELL, or None.
        """
        if current_bar.volume <= 0:
            return None

        # Upward move: close significantly above prior close
        if current_bar.close > prev_bar.close + self.directional_threshold:
            return Side.BUY

        # Downward move: close significantly below prior close
        if current_bar.close < prev_bar.close - self.directional_threshold:
            return Side.SELL

        return None

    def _detect_absorption_at_level(
        self, current_bar, prev_bar, session: int
    ) -> Optional[Tuple[float, float]]:
        """
        Detect absorption at a carried level.
        Returns (level_price, bar_extreme) or None.
        
        Absorption criteria:
        1. Price touched a carried level during the bar
        2. High one-way delta (>= absorption_delta_ratio of volume)
        3. Visible wick in opposite direction of delta
        """
        carried_levels = self._get_carried_levels(session)
        if not carried_levels:
            return None

        if current_bar.volume <= 0:
            return None

        # Find if any carried level is within the bar
        level_at_bar = None
        for level in carried_levels:
            if current_bar.low <= level <= current_bar.high:
                level_at_bar = level
                break

        if level_at_bar is None:
            return None

        # Check for sufficient one-way delta
        delta_ratio = abs(current_bar.delta) / current_bar.volume
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Detect absorption signature: wick without extension
        if current_bar.delta > 0:  # Buying absorption
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            if upper_wick >= 0.1:
                return (level_at_bar, current_bar.low)

        elif current_bar.delta < 0:  # Selling absorption
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            if lower_wick >= 0.1:
                return (level_at_bar, current_bar.high)

        return None
