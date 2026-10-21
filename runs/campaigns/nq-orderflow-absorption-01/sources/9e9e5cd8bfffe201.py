"""
Volume profile-based absorption strategy for NQ futures.

This strategy investigates whether absorption at carried volume levels precedes
moves to subsequent levels. Key design decisions:

1. Build a volume profile from 1-second bars (cheaper than ticks, sufficient for
   level identification) across the current session and carry forward the two
   prior sessions' profiles.

2. Track "naked" levels - prices where significant volume traded but price has
   not returned since formation.

3. Detect absorption: when price arrives at a carried level with aggressive
   one-way flow (high delta) but the bar closes back past the prior bar's
   extreme without extending it.

4. Use a structural stop just beyond the absorption bar's extreme (constant
   buffer), and target as a multiple of stop distance to reach the next level.

5. Optional confirmation: wait for an "initiation" bar showing efficient price
   movement (high delta per unit volume) and large body relative to recent range.

6. Tag entries separately for "absorption-only" vs "absorption-confirmed" to
   measure the cost/benefit of waiting.

7. Hold positions for multi-session moves between levels rather than scalping.

The cost model (0.2 points round-trip commission) requires multiple points per
trade, achievable only through structural levels and patient holding.
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
        volume_percentile: float = 0.7,
        absorption_threshold: float = 0.6,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_width: float = 3.0,
        use_confirmation: bool = False,
        efficiency_percentile: float = 0.75,
        min_body_ratio: float = 0.6,
        size: int = 1,
    ) -> None:
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.sessions_to_carry = sessions_to_carry
        self.volume_percentile = volume_percentile
        self.absorption_threshold = absorption_threshold
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_width = max_stop_width
        self.use_confirmation = use_confirmation
        self.efficiency_percentile = efficiency_percentile
        self.min_body_ratio = min_body_ratio
        self.size = size

        # State accumulated across bars
        self.session_profiles: Dict[int, Dict[float, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.current_session: Optional[int] = None
        self.session_volume: Dict[int, float] = defaultdict(float)

        # For initiation detection - use deque for bounded history
        self.recent_efficiency: deque = deque(maxlen=50)
        self.recent_range: deque = deque(maxlen=50)

        # Track previous slow bar for absorption detection
        self.prev_slow_bar = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Update session tracking
        if ctx.session != self.current_session:
            self.current_session = ctx.session
            self.session_volume[ctx.session] = 0.0

        # Process fast bars to accumulate volume profiles
        if self.fast_interval in ctx.bars:
            fast_bar = ctx.bars[self.fast_interval]
            self._update_profile(ctx.session, fast_bar)
            self._update_efficiency_tracking(fast_bar)

        # Decision logic on slow bars
        if self.slow_interval not in ctx.bars:
            return

        slow_bar = ctx.bars[self.slow_interval]

        if ctx.position or ctx.working_order_ids:
            self.prev_slow_bar = slow_bar
            return

        # Need previous bar for absorption detection
        if self.prev_slow_bar is None:
            self.prev_slow_bar = slow_bar
            return

        prev_slow = self.prev_slow_bar

        # Detect absorption at carried levels
        absorption_signal = self._detect_absorption(slow_bar, prev_slow, ctx.session)

        if absorption_signal is None:
            self.prev_slow_bar = slow_bar
            return

        level, side, bar_extreme = absorption_signal

        # Compute stop and target
        stop_distance = abs(bar_extreme - level) + self.stop_buffer

        if stop_distance > self.max_stop_width:
            self.prev_slow_bar = slow_bar
            return  # Decline setups with too-wide stops

        target_distance = stop_distance * self.target_multiple

        # Check for initiation if required
        if self.use_confirmation:
            if not self._check_initiation(slow_bar):
                self.prev_slow_bar = slow_bar
                return
            tag = "absorption-confirmed"
        else:
            tag = "absorption-only"

        # Place order
        if side == Side.BUY:
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

    def _update_profile(self, session: int, bar) -> None:
        """Accumulate volume at price levels from fast bars."""
        if bar.volume <= 0:
            return

        # Distribute bar volume across its range
        price_step = 0.25  # NQ tick size
        num_levels = max(1, int((bar.high - bar.low) / price_step) + 1)
        vol_per_level = bar.volume / num_levels

        price = bar.low
        while price <= bar.high:
            self.session_profiles[session][round(price, 2)] += vol_per_level
            self.session_volume[session] += vol_per_level
            price += price_step

    def _get_carried_levels(self, current_session: int) -> Dict[float, int]:
        """Get volume levels from current and prior sessions."""
        levels: Dict[float, int] = {}

        for offset in range(self.sessions_to_carry + 1):
            session = current_session - offset
            if session < 0:
                continue

            profile = self.session_profiles.get(session, {})
            if not profile:
                continue

            total_vol = self.session_volume.get(session, 0.0)
            if total_vol <= 0:
                continue

            # Find value area (70th percentile)
            sorted_prices = sorted(profile.keys())
            cumsum = 0.0
            threshold = total_vol * self.volume_percentile

            for price in sorted_prices:
                cumsum += profile[price]
                if cumsum >= threshold:
                    if price not in levels:
                        levels[price] = offset
                    break

        return levels

    def _detect_absorption(
        self, bar, prev_bar, session: int
    ) -> Optional[Tuple[float, Side, float]]:
        """
        Detect absorption: high delta at a carried level, bar closes back
        past prior extreme without extending it.

        Returns: (level_price, side, bar_extreme) or None
        """
        carried_levels = self._get_carried_levels(session)
        if not carried_levels:
            return None

        # Check if bar arrived at a carried level
        level_at_bar = None
        for level in carried_levels.keys():
            if bar.low <= level <= bar.high:
                level_at_bar = level
                break

        if level_at_bar is None:
            return None

        # Check for absorption signature
        delta = bar.delta
        if abs(delta) < self.absorption_threshold * bar.volume:
            return None  # Not enough one-way flow

        # Check if bar closes back past prior extreme
        if delta > 0:  # Buying pressure
            # For absorption, expect bar to not extend above prior high
            if bar.high <= prev_bar.high and bar.close < bar.open:
                # Absorption signature: high low, closes back down
                return (level_at_bar, Side.SELL, bar.low)
        else:  # Selling pressure
            # For absorption, expect bar to not extend below prior low
            if bar.low >= prev_bar.low and bar.close > bar.open:
                # Absorption signature: low high, closes back up
                return (level_at_bar, Side.BUY, bar.high)

        return None

    def _update_efficiency_tracking(self, bar) -> None:
        """Track bar efficiency (delta per volume) for initiation detection."""
        if bar.volume > 0:
            efficiency = abs(bar.delta) / bar.volume
            self.recent_efficiency.append((bar.delta, bar.volume, efficiency))

        range_size = bar.high - bar.low
        self.recent_range.append(range_size)

    def _check_initiation(self, bar) -> bool:
        """
        Check if bar shows initiation: high efficiency and large body
        relative to recent range.
        """
        if len(self.recent_efficiency) < 10:
            return False

        if bar.volume <= 0:
            return False

        current_efficiency = abs(bar.delta) / bar.volume

        # Compare to recent distribution (excluding current bar)
        recent_efficiencies = [e[2] for e in list(self.recent_efficiency)[:-1]]
        if not recent_efficiencies:
            return False

        sorted_eff = sorted(recent_efficiencies)
        threshold_idx = int(len(sorted_eff) * self.efficiency_percentile)
        threshold = sorted_eff[min(threshold_idx, len(sorted_eff) - 1)]

        if current_efficiency <= threshold:
            return False

        # Check body size relative to recent range
        body = abs(bar.close - bar.open)
        recent_range_list = list(self.recent_range)[-20:]
        if recent_range_list:
            avg_range = sum(recent_range_list) / len(recent_range_list)
        else:
            avg_range = 0.5

        if avg_range > 0 and body < self.min_body_ratio * avg_range:
            return False

        return True
