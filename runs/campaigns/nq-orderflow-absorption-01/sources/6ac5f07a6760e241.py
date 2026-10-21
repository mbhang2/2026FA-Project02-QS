"""
Volume profile absorption strategy - refined for consistent signal generation.

Prior trial #3 scored NOT SCORED due to exposure floor. The core issue was that
the strategy was too selective about what constitutes absorption, resulting in
very few trades. This revision focuses on:

1. More aggressive level identification: use all significant volume nodes, not
   just peak and value area boundaries
2. Relaxed absorption criteria: reduce delta_ratio threshold and simplify the
   tail detection logic
3. Better level proximity matching: increase tolerance for what "arrives at" a level
4. Simplified initiation check: focus on body size relative to range, not efficiency
5. Maintain the structural stop and multi-session holding period

The goal is to generate enough signals (aim for 50%+ exposure) while keeping the
core absorption hypothesis intact: that one-way flow meeting price at a volume
node precedes directional moves.
"""

from __future__ import annotations
from collections import defaultdict, deque
from typing import Dict, Optional, Tuple, Any

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1s",
        slow_interval: str = "5m",
        sessions_to_carry: int = 2,
        volume_bin_size: float = 0.25,
        volume_threshold_percentile: float = 0.5,
        absorption_delta_ratio: float = 0.5,
        level_proximity: float = 2.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_width: float = 3.5,
        use_confirmation: bool = False,
        min_body_ratio: float = 0.4,
        size: int = 1,
    ) -> None:
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.sessions_to_carry = sessions_to_carry
        self.volume_bin_size = volume_bin_size
        self.volume_threshold_percentile = volume_threshold_percentile
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_width = max_stop_width
        self.use_confirmation = use_confirmation
        self.min_body_ratio = min_body_ratio
        self.size = size

        # Session tracking
        self.current_session: Optional[int] = None

        # Volume profiles by session
        self.session_profiles: Dict[int, Dict[float, float]] = {}
        self.session_volumes: Dict[int, float] = {}

        # Recent slow bars for absorption detection
        self.recent_slow_bars: deque = deque(maxlen=50)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes and clean old profiles
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            # Keep only recent sessions' profiles
            sessions_to_keep = set(range(max(0, ctx.session - 3), ctx.session + 1))
            self.session_profiles = {
                s: p for s, p in self.session_profiles.items() if s in sessions_to_keep
            }
            self.session_volumes = {
                s: v for s, v in self.session_volumes.items() if s in sessions_to_keep
            }

        # Update volume profile from fast bars
        if self.fast_interval in ctx.bars:
            fast_bar = ctx.bars[self.fast_interval]
            self._update_profile(ctx.session, fast_bar)

        # Decision logic on slow bars
        if self.slow_interval not in ctx.bars:
            return

        slow_bar = ctx.bars[self.slow_interval]
        self.recent_slow_bars.append(slow_bar)

        # Don't trade if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Need at least 2 slow bars for comparison
        if len(self.recent_slow_bars) < 2:
            return

        prev_slow = self.recent_slow_bars[-2]
        current_slow = slow_bar

        # Detect absorption at carried levels
        absorption_result = self._detect_absorption(current_slow, prev_slow, ctx.session)

        if absorption_result is None:
            return

        level_price, direction, bar_extreme = absorption_result

        # Calculate stop distance from the absorption bar's extreme
        stop_distance = abs(bar_extreme - level_price) + self.stop_buffer

        # Decline setups with stops that are too wide
        if stop_distance > self.max_stop_width:
            return

        target_distance = stop_distance * self.target_multiple

        # Optional confirmation: check for initiation
        tag = "absorption-only"
        if self.use_confirmation:
            if not self._check_initiation(current_slow):
                return
            tag = "absorption-confirmed"

        # Place the order
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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_profile(self, session: int, bar: Any) -> None:
        """Build volume profile by binning volume at price levels."""
        if bar.volume <= 0:
            return

        # Initialize session if needed
        if session not in self.session_profiles:
            self.session_profiles[session] = {}
            self.session_volumes[session] = 0.0

        # Bin prices and distribute volume
        low_bin = (bar.low // self.volume_bin_size) * self.volume_bin_size
        high_bin = (bar.high // self.volume_bin_size) * self.volume_bin_size

        num_bins = max(1, int((high_bin - low_bin) / self.volume_bin_size) + 1)
        vol_per_bin = bar.volume / num_bins

        price = low_bin
        while price <= high_bin + self.volume_bin_size / 2:
            bin_key = round(price / self.volume_bin_size, 2) * self.volume_bin_size
            self.session_profiles[session][round(bin_key, 2)] = (
                self.session_profiles[session].get(round(bin_key, 2), 0.0) + vol_per_bin
            )
            self.session_volumes[session] += vol_per_bin
            price += self.volume_bin_size

    def _get_carried_levels(self, current_session: int) -> Dict[float, int]:
        """
        Extract significant volume levels from current and prior sessions.
        More aggressive: include all bins above threshold, not just peak/boundaries.
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
                if vol >= threshold and price not in levels:
                    levels[price] = offset

        return levels

    def _detect_absorption(
        self, current_bar: Any, prev_bar: Any, session: int
    ) -> Optional[Tuple[float, Side, float]]:
        """
        Detect absorption: price arrives near a carried level with high one-way
        delta and forms a tail (wick in opposite direction of delta).

        Returns (level_price, direction, bar_extreme) or None.
        """
        carried_levels = self._get_carried_levels(session)
        if not carried_levels:
            return None

        if current_bar.volume <= 0:
            return None

        # Find nearby carried level - more lenient matching
        level_at_bar: Optional[float] = None
        for level in carried_levels.keys():
            if abs(current_bar.close - level) <= self.level_proximity:
                level_at_bar = level
                break

        if level_at_bar is None:
            return None

        # Check for high one-way delta - relaxed threshold
        delta = current_bar.delta
        if delta == 0:
            return None

        delta_ratio = abs(delta) / current_bar.volume

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Detect absorption signature: wick in opposite direction of delta
        # Buying absorption: positive delta with upper wick
        if delta > 0:
            upper_wick = current_bar.high - max(current_bar.open, current_bar.close)
            # Require visible upper wick and bar doesn't extend significantly above prior
            if upper_wick > 0.1 and current_bar.high <= prev_bar.high + 1.0:
                return (level_at_bar, Side.SELL, current_bar.low)

        # Selling absorption: negative delta with lower wick
        elif delta < 0:
            lower_wick = min(current_bar.open, current_bar.close) - current_bar.low
            # Require visible lower wick and bar doesn't extend significantly below prior
            if lower_wick > 0.1 and current_bar.low >= prev_bar.low - 1.0:
                return (level_at_bar, Side.BUY, current_bar.high)

        return None

    def _check_initiation(self, bar: Any) -> bool:
        """
        Check if bar shows initiation: large body relative to recent range.
        """
        if len(self.recent_slow_bars) < 3:
            return False

        if bar.volume <= 0:
            return False

        # Body size relative to recent bars
        body = abs(bar.close - bar.open)

        recent_bars_list = list(self.recent_slow_bars)[:-1]
        if not recent_bars_list:
            return False

        recent_ranges = [b.high - b.low for b in recent_bars_list[-10:]]
        avg_range = sum(recent_ranges) / len(recent_ranges) if recent_ranges else 0.5

        if avg_range <= 0:
            return False

        body_ratio = body / avg_range

        # Require body to be a significant fraction of recent range
        return body_ratio >= self.min_body_ratio
