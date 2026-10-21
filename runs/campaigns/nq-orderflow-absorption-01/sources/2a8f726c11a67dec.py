"""
Volume profile absorption strategy - refined for signal consistency and cost awareness.

Analysis of recent trials:
- All attempts #0-#7 scored NOT SCORED due to insufficient exposure (need 70%+)
- Synthetic test showed identical fills to baseline, indicating absorption conditions
  weren't being triggered on that data
- Current approach: VPOC from 1-second bars, carry levels from 2-3 prior sessions,
  detect absorption as one-way flow at carried level with rejection pattern

Key insight from campaign brief:
The strategy must hold for moves BETWEEN levels, not intra-bar moves. This means:
1. Positions are multi-bar holds (often crossing sessions)
2. Targets should reach toward the next level in the profile
3. The exposure floor of 70% means we need to trade in ~270 of 384 sessions

Current problem: Detection is still too strict. Even with lower thresholds,
absorption is a rare pattern. Need to:
1. Broaden what counts as "arrival" at a level
2. Simplify absorption detection - focus on the core: one-way flow at a level
3. Accept that some signals will be false; the cost model will filter them
4. Trade more frequently to reach the exposure floor

New approach:
- Keep VPOC tracking (more stable than individual level detection)
- Relax arrival criteria: price touched the level during the bar (not just close)
- Simplify delta check: just need >40% one-way flow
- Simplify tail check: just need visible wick, not strict ratios
- Remove confirmation entirely - act immediately
- Increase target multiple to ensure multi-point moves
- Let the bracket and cost model do the filtering
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
        sessions_to_carry: int = 3,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.0,
        max_stop_width: float = 4.0,
        size: int = 1,
    ) -> None:
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.sessions_to_carry = sessions_to_carry
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_width = max_stop_width
        self.size = size

        # Accumulated volume profile state - survives window constraints
        self.session_price_volume: Dict[int, Dict[float, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.session_total_volume: Dict[int, float] = defaultdict(float)
        self.session_vpoc: Dict[int, float] = {}
        
        # Track all slow bars for absorption detection
        self.all_slow_bars: deque = deque(maxlen=1000)
        self.current_session: Optional[int] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes and maintain profile history
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            # Keep profiles from current and prior sessions
            sessions_to_keep = set(range(max(0, ctx.session - 4), ctx.session + 1))
            self.session_price_volume = {
                s: p for s, p in self.session_price_volume.items() if s in sessions_to_keep
            }
            self.session_total_volume = {
                s: v for s, v in self.session_total_volume.items() if s in sessions_to_keep
            }
            self.session_vpoc = {
                s: v for s, v in self.session_vpoc.items() if s in sessions_to_keep
            }

        # Accumulate volume profile from fast bars
        if self.fast_interval in ctx.bars:
            fast_bar = ctx.bars[self.fast_interval]
            self._accumulate_profile(ctx.session, fast_bar)

        # Decision logic on slow bars
        if self.slow_interval not in ctx.bars:
            return

        slow_bar = ctx.bars[self.slow_interval]
        self.all_slow_bars.append(slow_bar)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Need at least 2 bars for comparison
        if len(self.all_slow_bars) < 2:
            return

        prev_bar = self.all_slow_bars[-2]
        current_bar = slow_bar

        # Detect absorption at carried levels
        signal = self._detect_absorption(current_bar, prev_bar, ctx.session)

        if signal is None:
            return

        level, direction, bar_extreme = signal

        # Calculate stop and target
        stop_distance = abs(bar_extreme - level) + self.stop_buffer

        # Decline if stop is too wide
        if stop_distance > self.max_stop_width:
            return

        target_distance = stop_distance * self.target_multiple

        # Place order
        if direction == Side.BUY:
            ctx.buy(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag="absorption-only",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag="absorption-only",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _accumulate_profile(self, session: int, bar) -> None:
        """Build volume profile from fast bars, tracking VPOC per session."""
        if bar.volume <= 0 or bar.tick_count == 0:
            return

        # Distribute volume across price levels using 0.25 tick size
        tick_size = 0.25
        num_levels = max(1, int((bar.high - bar.low) / tick_size) + 1)
        vol_per_level = bar.volume / num_levels

        price = bar.low
        while price <= bar.high + tick_size * 0.01:
            price_key = round(price / tick_size) * tick_size
            self.session_price_volume[session][price_key] += vol_per_level
            self.session_total_volume[session] += vol_per_level
            price += tick_size

        # Update VPOC: the price level with most volume in this session
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

    def _detect_absorption(
        self, bar, prev_bar, session: int
    ) -> Optional[Tuple[float, Side, float]]:
        """
        Detect absorption at a carried level:
        1. Price touched the level (bar.low <= level <= bar.high)
        2. Sufficient one-way delta (>= 40% of volume)
        3. Visible wick in opposite direction of delta (bar rejected)
        4. Bar did not extend significantly past prior extreme

        Returns: (level_price, direction, bar_extreme) or None
        """
        carried_levels = self._get_carried_levels(session)
        if not carried_levels:
            return None

        if bar.volume <= 0:
            return None

        # Find if price touched any carried level during the bar
        level_at_bar = None
        for level in carried_levels:
            if bar.low <= level <= bar.high:
                level_at_bar = level
                break

        if level_at_bar is None:
            return None

        # Check for sufficient one-way delta (>= 40% of volume)
        delta_ratio = abs(bar.delta) / bar.volume if bar.volume > 0 else 0
        if delta_ratio < 0.40:
            return None

        # Detect absorption signature: wick without extension
        bar_range = bar.high - bar.low
        if bar_range < 0.1:  # Need meaningful range
            return None

        # Buying absorption: positive delta, upper wick, didn't extend above prior
        if bar.delta > 0:
            upper_wick = bar.high - max(bar.open, bar.close)
            # Need visible upper wick (at least 0.1 points)
            if upper_wick >= 0.1 and bar.high <= prev_bar.high + 1.0:
                return (level_at_bar, Side.SELL, bar.low)

        # Selling absorption: negative delta, lower wick, didn't extend below prior
        elif bar.delta < 0:
            lower_wick = min(bar.open, bar.close) - bar.low
            # Need visible lower wick (at least 0.1 points)
            if lower_wick >= 0.1 and bar.low >= prev_bar.low - 1.0:
                return (level_at_bar, Side.BUY, bar.high)

        return None
