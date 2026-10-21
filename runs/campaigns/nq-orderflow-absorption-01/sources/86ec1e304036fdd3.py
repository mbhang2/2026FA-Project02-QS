"""
Volume profile absorption strategy - focused on structural levels and cost awareness.

The campaign investigates whether absorption at carried volume levels precedes
moves between levels. With a 0.2-point round-trip cost, profitability requires
multi-point moves, not scalping.

Key design:
1. Build VPOC (volume point of control) from 1-second bars across current and
   prior sessions - the actual level where volume traded.
2. Detect absorption: price arrives at a carried level + high delta (one-way flow)
   + tail formation (rejection without follow-through).
3. Structural stop: constant buffer past the absorption bar's extreme (not tuned).
4. Target as a multiple of stop distance to reach the next level.
5. Optional confirmation: wait for initiation bar showing efficient price movement
   and large body relative to recent range.
6. Tag entries separately to measure cost/benefit of waiting.
7. Hold for multi-session moves between levels, not intra-session scalps.

The synthetic test showed identical fills to the baseline, which means the
conditions for absorption detection were not met on that data. This revision
focuses on making the absorption detection more reliable and easier to trigger
on real data while maintaining structural integrity.
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
        absorption_delta_ratio: float = 0.55,
        absorption_tail_ratio: float = 0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_width: float = 3.0,
        use_confirmation: bool = False,
        min_body_ratio: float = 0.6,
        size: int = 1,
    ) -> None:
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.sessions_to_carry = sessions_to_carry
        self.absorption_delta_ratio = absorption_delta_ratio
        self.absorption_tail_ratio = absorption_tail_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_width = max_stop_width
        self.use_confirmation = use_confirmation
        self.min_body_ratio = min_body_ratio
        self.size = size

        # Accumulated state - survives window constraints
        self.session_vpoc: Dict[int, float] = {}
        self.session_volumes: Dict[int, float] = defaultdict(float)
        self.price_volume: Dict[int, Dict[float, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.current_session: Optional[int] = None
        self.prev_slow_bar = None

        # Efficiency tracking for initiation
        self.recent_bars: deque = deque(maxlen=25)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session

        # Accumulate volume profile from fast bars
        if self.fast_interval in ctx.bars:
            fast_bar = ctx.bars[self.fast_interval]
            self._accumulate_profile(ctx.session, fast_bar)

        # Decision logic on slow bars
        if self.slow_interval not in ctx.bars:
            return

        slow_bar = ctx.bars[self.slow_interval]

        if ctx.position or ctx.working_order_ids:
            self.prev_slow_bar = slow_bar
            self.recent_bars.append(slow_bar)
            return

        # Need previous bar for proper absorption detection
        if self.prev_slow_bar is None:
            self.prev_slow_bar = slow_bar
            self.recent_bars.append(slow_bar)
            return

        prev_bar = self.prev_slow_bar

        # Detect absorption at carried levels
        signal = self._detect_absorption(slow_bar, prev_bar, ctx.session)

        if signal is None:
            self.prev_slow_bar = slow_bar
            self.recent_bars.append(slow_bar)
            return

        level, direction, bar_extreme = signal

        # Calculate stop and target
        stop_distance = abs(bar_extreme - level) + self.stop_buffer

        if stop_distance > self.max_stop_width:
            self.prev_slow_bar = slow_bar
            self.recent_bars.append(slow_bar)
            return

        target_distance = stop_distance * self.target_multiple

        # Optional confirmation
        tag = "absorption-only"
        if self.use_confirmation:
            if not self._check_initiation(slow_bar):
                self.prev_slow_bar = slow_bar
                self.recent_bars.append(slow_bar)
                return
            tag = "absorption-confirmed"

        # Track this bar for efficiency history
        self.recent_bars.append(slow_bar)

        # Place order
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
        """Build volume profile from fast bars, tracking VPOC."""
        if bar.volume <= 0 or bar.tick_count == 0:
            return

        # Distribute volume across price levels
        tick_size = 0.25
        num_levels = max(1, int((bar.high - bar.low) / tick_size) + 1)
        vol_per_level = bar.volume / num_levels

        price = bar.low
        while price <= bar.high + tick_size * 0.01:
            price_key = round(price / tick_size) * tick_size
            self.price_volume[session][price_key] += vol_per_level
            self.session_volumes[session] += vol_per_level
            price += tick_size

        # Update VPOC for this session
        if self.price_volume[session]:
            vpoc = max(
                self.price_volume[session].items(), key=lambda x: x[1]
            )[0]
            self.session_vpoc[session] = vpoc

    def _get_carried_levels(self, current_session: int) -> Dict[float, int]:
        """Get VPOC levels from current and prior sessions."""
        levels: Dict[float, int] = {}

        for offset in range(self.sessions_to_carry + 1):
            session = current_session - offset
            if session < 0:
                continue

            vpoc = self.session_vpoc.get(session)
            if vpoc is not None:
                levels[vpoc] = offset

        return levels

    def _detect_absorption(
        self, bar, prev_bar, session: int
    ) -> Optional[Tuple[float, Side, float]]:
        """
        Detect absorption: price arrives at carried level with high delta,
        bar shows rejection (tail) without extending prior extreme.

        Returns: (level_price, direction, bar_extreme) or None
        """
        carried_levels = self._get_carried_levels(session)
        if not carried_levels:
            return None

        # Check if price arrived at a carried level
        level_at_bar = None
        for level in carried_levels.keys():
            if bar.low <= level <= bar.high:
                level_at_bar = level
                break

        if level_at_bar is None:
            return None

        # Check for sufficient one-way flow (delta)
        if abs(bar.delta) < self.absorption_delta_ratio * bar.volume:
            return None

        # Check for absorption signature: rejection without extension
        if bar.delta > 0:  # Buying pressure
            # Expect: tail below close, doesn't extend above prior high
            tail_size = bar.high - bar.close
            bar_range = bar.high - bar.low
            if bar_range > 0 and tail_size >= self.absorption_tail_ratio * bar_range:
                if bar.high <= prev_bar.high:
                    # Absorption: bought but rejected, didn't extend up
                    return (level_at_bar, Side.SELL, bar.low)

        else:  # Selling pressure (bar.delta < 0)
            # Expect: tail above close, doesn't extend below prior low
            tail_size = bar.close - bar.low
            bar_range = bar.high - bar.low
            if bar_range > 0 and tail_size >= self.absorption_tail_ratio * bar_range:
                if bar.low >= prev_bar.low:
                    # Absorption: sold but rejected, didn't extend down
                    return (level_at_bar, Side.BUY, bar.high)

        return None

    def _check_initiation(self, bar) -> bool:
        """
        Check if bar shows initiation: efficient price movement relative to
        flow direction, and large body relative to recent range.
        """
        if len(self.recent_bars) < 5:
            return False

        if bar.volume <= 0 or bar.delta == 0:
            return False

        # Efficiency: body size per unit of directional flow
        body = abs(bar.close - bar.open)
        efficiency = body / abs(bar.delta)

        # Compare to recent bars with same flow direction
        same_direction_efficiencies: list[float] = []
        for recent in list(self.recent_bars)[-10:]:
            if recent.delta * bar.delta > 0:  # Same sign
                if recent.volume > 0 and recent.delta != 0:
                    eff = abs(recent.close - recent.open) / abs(recent.delta)
                    same_direction_efficiencies.append(eff)

        if not same_direction_efficiencies:
            # No recent bars with same direction; use absolute threshold
            if efficiency < 0.25:
                return False
        else:
            # Must be above 55th percentile of same-direction bars
            sorted_eff = sorted(same_direction_efficiencies)
            threshold_idx = max(0, int(0.55 * len(sorted_eff)))
            threshold = sorted_eff[min(threshold_idx, len(sorted_eff) - 1)]
            if efficiency <= threshold:
                return False

        # Check body size relative to recent range
        recent_bars_list = list(self.recent_bars)[-15:]
        if recent_bars_list:
            avg_range = sum(b.high - b.low for b in recent_bars_list) / len(
                recent_bars_list
            )
        else:
            avg_range = 0.5

        if avg_range > 0 and body < self.min_body_ratio * avg_range:
            return False

        return True
