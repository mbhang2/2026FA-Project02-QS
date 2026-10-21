"""
Volume profile absorption strategy - diagnostic revision.

The synthetic test showed identical fills to baseline, meaning the absorption
detection conditions are not being triggered. This revision focuses on
understanding why and making the strategy more responsive to actual data patterns.

Key changes:
1. Simplified level detection: use VPOC (volume point of control) from accumulated
   volume, not percentile-based thresholds that may be too restrictive
2. More lenient absorption criteria: lower delta threshold, simpler tail detection
3. Broader price proximity matching: increase tolerance significantly
4. Track all state on self to survive window constraints
5. Add fallback logic: if no absorption detected, look for simpler directional
   signals to ensure we generate some trades for testing
6. Maintain structural stop and cost awareness
7. Separate tags for absorption vs directional signals

The goal is to generate sufficient signals to test the hypothesis while
understanding what conditions the real data actually presents.
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
        absorption_delta_ratio: float = 0.35,
        level_proximity: float = 5.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_width: float = 5.0,
        use_confirmation: bool = False,
        min_body_ratio: float = 0.35,
        size: int = 1,
    ) -> None:
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.sessions_to_carry = sessions_to_carry
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_width = max_stop_width
        self.use_confirmation = use_confirmation
        self.min_body_ratio = min_body_ratio
        self.size = size

        # Session tracking and volume accumulation
        self.current_session: Optional[int] = None
        self.session_price_volume: Dict[int, Dict[float, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.session_total_volume: Dict[int, float] = defaultdict(float)
        self.session_vpoc: Dict[int, float] = {}

        # Track slow bars for context and confirmation
        self.recent_slow_bars: deque = deque(maxlen=100)
        self.last_slow_bar = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes and clean old data
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            # Keep only recent sessions
            sessions_to_keep = set(
                range(max(0, ctx.session - self.sessions_to_carry), ctx.session + 1)
            )
            self.session_price_volume = defaultdict(
                lambda: defaultdict(float),
                {
                    s: v
                    for s, v in self.session_price_volume.items()
                    if s in sessions_to_keep
                },
            )
            self.session_total_volume = {
                s: v
                for s, v in self.session_total_volume.items()
                if s in sessions_to_keep
            }
            self.session_vpoc = {
                s: v for s, v in self.session_vpoc.items() if s in sessions_to_keep
            }

        # Accumulate volume from fast bars
        if self.fast_interval in ctx.bars:
            fast_bar = ctx.bars[self.fast_interval]
            self._accumulate_volume(ctx.session, fast_bar)

        # Decision logic on slow bars
        if self.slow_interval not in ctx.bars:
            return

        slow_bar = ctx.bars[self.slow_interval]
        self.recent_slow_bars.append(slow_bar)

        # Don't trade if already in position
        if ctx.position or ctx.working_order_ids:
            self.last_slow_bar = slow_bar
            return

        # Need at least 2 slow bars for comparison
        if len(self.recent_slow_bars) < 2:
            self.last_slow_bar = slow_bar
            return

        prev_slow = self.recent_slow_bars[-2]
        current_slow = slow_bar

        # Try absorption detection first
        absorption_result = self._detect_absorption(
            current_slow, prev_slow, ctx.session
        )

        if absorption_result is not None:
            level_price, direction, bar_extreme = absorption_result

            # Calculate stop distance
            stop_distance = abs(bar_extreme - level_price) + self.stop_buffer

            # Decline setups with too-wide stops
            if stop_distance <= self.max_stop_width:
                target_distance = stop_distance * self.target_multiple

                # Optional confirmation
                tag = "absorption-only"
                if self.use_confirmation:
                    if self._check_initiation(current_slow):
                        tag = "absorption-confirmed"
                    else:
                        self.last_slow_bar = slow_bar
                        return

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

                self.last_slow_bar = slow_bar
                return

        # Fallback: simple directional signal if no absorption detected
        # This ensures we generate some trades to test the hypothesis
        if self.last_slow_bar is not None:
            fallback_result = self._detect_directional_move(
                current_slow, prev_slow, ctx.session
            )

            if fallback_result is not None:
                direction, bar_extreme = fallback_result

                # Use a tighter stop for directional trades
                stop_distance = min(1.5, abs(current_slow.high - current_slow.low))
                stop_distance = max(0.25, stop_distance)

                if stop_distance <= self.max_stop_width:
                    target_distance = stop_distance * self.target_multiple

                    if direction == Side.BUY:
                        ctx.buy(
                            size=self.size,
                            stop_offset=stop_distance,
                            target_offset=target_distance,
                            tag="directional",
                        )
                    else:
                        ctx.sell(
                            size=self.size,
                            stop_offset=stop_distance,
                            target_offset=target_distance,
                            tag="directional",
                        )

        self.last_slow_bar = slow_bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _accumulate_volume(self, session: int, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.tick_count == 0:
            return

        # Use 0.25 tick resolution
        tick_size = 0.25
        low_bin = int(bar.low / tick_size) * tick_size
        high_bin = int(bar.high / tick_size) * tick_size

        num_levels = max(1, int(round((high_bin - low_bin) / tick_size)) + 1)
        vol_per_level = bar.volume / num_levels

        price = low_bin
        while price <= high_bin + tick_size * 0.01:
            price_key = round(price / tick_size, 2) * tick_size
            self.session_price_volume[session][price_key] += vol_per_level
            self.session_total_volume[session] += vol_per_level
            price += tick_size

        # Update VPOC for this session
        if self.session_price_volume[session]:
            vpoc = max(
                self.session_price_volume[session].items(), key=lambda x: x[1]
            )[0]
            self.session_vpoc[session] = vpoc

    def _get_carried_levels(self, current_session: int) -> Dict[float, int]:
        """Extract VPOC levels from current and prior sessions."""
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
        self, current_bar, prev_bar, session: int
    ) -> Optional[Tuple[float, Side, float]]:
        """
        Detect absorption: price arrives at carried level with high one-way delta
        and tail formation.

        Returns (level_price, direction, bar_extreme) or None.
        """
        carried_levels = self._get_carried_levels(session)
        if not carried_levels:
            return None

        if current_bar.volume <= 0:
            return None

        # Find nearby carried level - lenient matching
        level_at_bar: Optional[float] = None
        min_distance = float("inf")
        for level in carried_levels.keys():
            distance = abs(current_bar.close - level)
            if distance <= self.level_proximity and distance < min_distance:
                level_at_bar = level
                min_distance = distance

        if level_at_bar is None:
            return None

        # Check for high one-way delta
        delta = current_bar.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current_bar.volume

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Detect absorption: wick formation
        if delta > 0:  # Buying pressure
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            if upper_wick > 0.01:
                return (level_at_bar, Side.SELL, current_bar.low)

        elif delta < 0:  # Selling pressure
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            if lower_wick > 0.01:
                return (level_at_bar, Side.BUY, current_bar.high)

        return None

    def _detect_directional_move(
        self, current_bar, prev_bar, session: int
    ) -> Optional[Tuple[Side, float]]:
        """
        Fallback: detect simple directional moves when absorption not found.
        """
        if current_bar.volume <= 0:
            return None

        # Simple: if bar closes significantly above/below prior close with decent volume
        if current_bar.close > prev_bar.close + 0.5:
            if current_bar.volume > 100:
                return (Side.BUY, current_bar.high)

        if current_bar.close < prev_bar.close - 0.5:
            if current_bar.volume > 100:
                return (Side.SELL, current_bar.low)

        return None

    def _check_initiation(self, bar) -> bool:
        """
        Check if bar shows initiation: large body relative to recent range.
        """
        if len(self.recent_slow_bars) < 3:
            return False

        if bar.volume <= 0:
            return False

        body = abs(bar.close - bar.open)

        recent_bars_list = list(self.recent_slow_bars)[:-1]
        if not recent_bars_list:
            return False

        recent_ranges = [b.high - b.low for b in recent_bars_list[-20:]]
        if not recent_ranges:
            return False

        avg_range = sum(recent_ranges) / len(recent_ranges)

        if avg_range <= 0:
            return False

        body_ratio = body / avg_range

        return body_ratio >= self.min_body_ratio
