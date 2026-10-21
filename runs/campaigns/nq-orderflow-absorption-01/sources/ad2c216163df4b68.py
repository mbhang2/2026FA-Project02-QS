"""
Volume profile absorption strategy with refined level detection and signal reliability.

Key refinements from prior attempts:
1. Build volume profile from accumulated 5m bars (survives window boundaries via self).
2. Identify high-volume nodes as potential support/resistance levels.
3. Track "naked" levels: high-volume prices not yet returned to in current session.
4. Detect absorption: price arrives at naked level + strong one-way delta + bar fails
   to extend and closes back (tail formation).
5. Structural stop just beyond absorption bar extreme + fixed buffer (not tuned).
6. Target as multiple of stop distance to reach next level.
7. Keep positions one at a time; bracket handles exits.
8. Tag entries as "absorption" for measurement.
9. Accumulate all state on self to survive history window boundaries.

The core hypothesis: absorption at a carried level (price arrives with strong flow
but fails to push through) precedes a move away from that level to the next node.
"""

from __future__ import annotations
from collections import defaultdict, deque
from typing import Dict, Optional, Tuple

from src.core.types import Side


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        sessions_to_carry: int = 2,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_width: float = 3.0,
        delta_threshold_mult: float = 1.2,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.sessions_to_carry = sessions_to_carry
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_width = max_stop_width
        self.delta_threshold_mult = delta_threshold_mult
        self.size = size

        # Accumulate all bars to build profiles across sessions
        self.all_bars: list = []
        self.current_session: Optional[int] = None
        
        # Volume profiles: session -> price -> volume
        self.session_profiles: Dict[int, Dict[float, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.session_total_volume: Dict[int, float] = {}
        
        # Track which levels have been touched (price returned) in each session
        self.touched_levels: Dict[int, set] = defaultdict(set)
        
        # Recent delta distribution for dynamic threshold
        self.recent_deltas: deque = deque(maxlen=100)
        
        # Previous bar for absorption detection
        self.prev_bar: Optional[object] = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session changes
        if ctx.session != self.current_session:
            self.current_session = ctx.session

        # Get the bar that just closed
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]
        self.all_bars.append(bar)
        
        # Update profile and recent delta tracking
        self._update_profile(ctx.session, bar)
        if bar.volume > 0:
            self.recent_deltas.append(abs(bar.delta))

        # Already have position or working orders
        if ctx.position or ctx.working_order_ids:
            self._update_touched_levels(ctx.session, bar)
            self.prev_bar = bar
            return

        # Need previous bar for absorption detection
        if self.prev_bar is None:
            self._update_touched_levels(ctx.session, bar)
            self.prev_bar = bar
            return

        prev_bar = self.prev_bar

        # Detect absorption at carried naked levels
        absorption_signal = self._detect_absorption(bar, prev_bar, ctx.session)

        if absorption_signal is not None:
            level, side, bar_extreme = absorption_signal
            stop_distance = abs(bar_extreme - level) + self.stop_buffer

            # Decline setups with stops that are too wide
            if stop_distance <= self.max_stop_width:
                target_distance = stop_distance * self.target_multiple
                
                if side == Side.BUY:
                    ctx.buy(
                        size=self.size,
                        stop_offset=stop_distance,
                        target_offset=target_distance,
                        tag="absorption",
                    )
                else:
                    ctx.sell(
                        size=self.size,
                        stop_offset=stop_distance,
                        target_offset=target_distance,
                        tag="absorption",
                    )

        self._update_touched_levels(ctx.session, bar)
        self.prev_bar = bar

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_profile(self, session: int, bar) -> None:
        """Build volume profile from bars, distributing volume across price levels."""
        if bar.volume <= 0 or bar.tick_count == 0:
            return

        # Distribute volume across price levels touched by the bar
        price_step = 0.25  # NQ tick size
        num_levels = max(1, int((bar.high - bar.low) / price_step) + 1)
        vol_per_level = bar.volume / num_levels

        price = bar.low
        while price <= bar.high + 1e-9:
            rounded_price = round(price / price_step) * price_step
            self.session_profiles[session][rounded_price] += vol_per_level
            price += price_step

        if session not in self.session_total_volume:
            self.session_total_volume[session] = 0.0
        self.session_total_volume[session] += bar.volume

    def _get_naked_levels(self, current_session: int) -> Dict[float, int]:
        """
        Get naked levels: high-volume prices that have not been returned to
        since they formed, from current and prior sessions.
        
        Returns dict of {price: session_offset} for naked levels.
        """
        naked: Dict[float, int] = {}

        for offset in range(self.sessions_to_carry + 1):
            session = current_session - offset
            if session < 0:
                continue

            profile = self.session_profiles.get(session, {})
            if not profile:
                continue

            total_vol = self.session_total_volume.get(session, 0.0)
            if total_vol <= 0:
                continue

            # Find high-volume levels (top quartile by volume)
            sorted_prices = sorted(profile.items(), key=lambda x: x[1], reverse=True)
            threshold_idx = max(1, len(sorted_prices) // 4)
            
            touched = self.touched_levels.get(session, set())
            
            # Naked = high volume AND not yet touched in this session
            for price, vol in sorted_prices[:threshold_idx]:
                if price not in touched:
                    if price not in naked:
                        naked[price] = offset

        return naked

    def _update_touched_levels(self, session: int, bar) -> None:
        """Mark levels as touched when price returns to them."""
        for price in list(self.session_profiles[session].keys()):
            if bar.low <= price <= bar.high:
                self.touched_levels[session].add(price)

    def _detect_absorption(
        self, bar, prev_bar, session: int
    ) -> Optional[Tuple[float, Side, float]]:
        """
        Detect absorption at a naked level:
        - Price arrives at a naked level (bar spans the level)
        - High one-way delta relative to recent bars
        - Bar fails to extend past prior extreme and closes back (tail formation)
        
        Returns (level_price, side, bar_extreme) or None.
        """
        naked_levels = self._get_naked_levels(session)
        if not naked_levels:
            return None

        # Find which naked level price arrived at
        level_at_bar = None
        for level in naked_levels.keys():
            if bar.low <= level <= bar.high:
                level_at_bar = level
                break

        if level_at_bar is None:
            return None

        # Check for high one-way flow relative to recent bars
        if len(self.recent_deltas) < 10:
            avg_delta = 0.5 * bar.volume
        else:
            avg_delta = sum(self.recent_deltas) / len(self.recent_deltas)

        delta_abs = abs(bar.delta)
        if delta_abs < self.delta_threshold_mult * avg_delta:
            return None

        # Check absorption signature: tail formation
        # Bar arrives at level with strong flow but closes back without extending
        if bar.delta > 0:  # Buying pressure (positive delta)
            # For absorption: high doesn't extend above prev high,
            # close is below open (tail down = absorption of buy)
            if bar.high <= prev_bar.high and bar.close < bar.open:
                # Buying was absorbed, expect move down (sell signal)
                return (level_at_bar, Side.SELL, bar.low)
        else:  # Selling pressure (negative delta)
            # For absorption: low doesn't extend below prev low,
            # close is above open (tail up = absorption of sell)
            if bar.low >= prev_bar.low and bar.close > bar.open:
                # Selling was absorbed, expect move up (buy signal)
                return (level_at_bar, Side.BUY, bar.high)

        return None
