"""
Volume profile absorption strategy - addressing the floor constraint.

The prior attempts were rejected for insufficient exposure (< 50% of sessions).
This version focuses on increasing signal frequency while maintaining quality.

Key changes:
1. Relax absorption detection: require 65% (not 75%) of volume one-way
2. Lower stop distance bounds: accept stops from 0.15 to 1.5 points (was 0.25-2.0)
3. Expand level detection: include more granular price levels from profile
4. Remove initiation requirement by default: act on absorption alone
5. Increase pending setup window: hold setups for 6 bars (was 4)
6. Better session-level profile accumulation to find more tradeable levels

The structural stop remains at the absorption bar extreme + fixed buffer.
Targets scale as a multiple of stop distance. Entries are tagged by type.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional


@dataclass
class PriceLevel:
    """A price level with volume from a session."""
    price: float
    volume: int
    session: int


@dataclass
class AbsorptionSignal:
    """Detected absorption setup ready for entry."""
    session: int
    timestamp: int
    level: float
    direction: str  # "long" or "short"
    absorption_bar_close: float
    absorption_bar_high: float
    absorption_bar_low: float
    delta_volume: int
    stop_distance: float
    bars_since_detection: int = 0


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback_sessions: int = 2,
        absorption_min_ratio: float = 0.65,
        stop_buffer: float = 0.15,
        target_multiple: float = 2.0,
        max_stop_distance: float = 1.5,
        min_stop_distance: float = 0.15,
        require_initiation: bool = False,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback_sessions = lookback_sessions
        self.absorption_min_ratio = absorption_min_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.require_initiation = require_initiation
        self.size = size

        # Session and bar tracking
        self._current_session: Optional[int] = None
        
        # Accumulated price levels from prior sessions
        self._prior_levels: list[PriceLevel] = []
        
        # Current session profile: price -> volume
        self._current_profile: dict[float, int] = {}
        
        # Pending absorption setups
        self._pending_setups: deque[AbsorptionSignal] = deque(maxlen=10)
        
        # Efficiency tracking for initiation
        self._recent_efficiencies: deque[float] = deque(maxlen=12)
        self._recent_ranges: deque[float] = deque(maxlen=12)
        
        # Track last absorption detection to avoid duplicates
        self._last_absorption_ts: int = -1

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current = history[-1]

        # Handle session transitions
        if current.session != self._current_session:
            self._transition_session(current.session)

        # Accumulate bars for profile
        self._update_current_profile(current)

        # Update efficiency tracking
        self._update_efficiency(current)

        # Increment bar counters for pending setups
        for setup in self._pending_setups:
            setup.bars_since_detection += 1

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check for new absorption (only once per bar)
        if current.close_ts != self._last_absorption_ts:
            new_signal = self._detect_absorption(current, history)
            if new_signal:
                self._pending_setups.append(new_signal)
                self._last_absorption_ts = current.close_ts

        # Process pending setups
        self._process_pending_setups(ctx, current)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _transition_session(self, new_session: int) -> None:
        """Handle session change: carry forward top volume levels."""
        # Extract highest-volume levels from completed session
        if self._current_profile and self._current_session is not None:
            sorted_levels = sorted(
                self._current_profile.items(),
                key=lambda x: x[1],
                reverse=True
            )
            # Keep top 8 levels for next session (expanded from 5)
            self._prior_levels = [
                PriceLevel(price, vol, self._current_session)
                for price, vol in sorted_levels[:8]
            ]

        self._current_session = new_session
        self._current_profile = {}
        self._pending_setups.clear()
        self._recent_efficiencies.clear()
        self._recent_ranges.clear()
        self._last_absorption_ts = -1

    def _update_current_profile(self, bar) -> None:
        """Accumulate volume at price levels from current session bars."""
        if bar.volume == 0 or bar.tick_count == 0:
            return

        # Use finer granularity: 0.1 point levels instead of 0.25
        # This creates more levels to detect absorption at
        num_levels = max(1, int((bar.high - bar.low) / 0.1) + 1)
        vol_per_level = bar.volume / num_levels if num_levels > 0 else 0

        for i in range(num_levels):
            price = bar.low + (i * (bar.high - bar.low) / num_levels)
            level = round(price * 10) / 10
            self._current_profile[level] = self._current_profile.get(level, 0) + int(vol_per_level)

    def _update_efficiency(self, bar) -> None:
        """Track bar efficiency (range per unit flow) for initiation confirmation."""
        if bar.volume <= 0 or bar.high == bar.low:
            return

        range_traveled = bar.high - bar.low
        self._recent_ranges.append(range_traveled)

        # Efficiency: range per unit absolute flow
        abs_delta = abs(bar.delta)
        if abs_delta > 0:
            efficiency = range_traveled / abs_delta
            self._recent_efficiencies.append(efficiency)

    def _detect_absorption(self, current, history) -> Optional[AbsorptionSignal]:
        """
        Detect absorption: strong one-way flow at a carried level with clear reversal.
        
        Absorption requires:
        1. Strong directional delta (65%+ of volume one-way, lowered from 75%)
        2. Price arrival at a carried level
        3. Clear reversal (close opposite to flow direction)
        4. Reasonable stop distance (0.15 to 1.5 points)
        """
        if len(history) < 2 or current.volume == 0:
            return None

        prior = history[-2]
        delta = current.delta

        # Require strong directional flow (65%+ of volume)
        if abs(delta) < current.volume * self.absorption_min_ratio:
            return None

        # Get all relevant levels to check
        all_levels = [level.price for level in self._prior_levels]
        all_levels.extend(self._current_profile.keys())

        if not all_levels:
            return None

        for level in all_levels:
            # Check if price arrived at this level (within the bar's range)
            arrived_at_level = (
                min(current.low, prior.high) <= level <= max(current.high, prior.low)
            )
            if not arrived_at_level:
                continue

            # Long absorption: strong buying (delta > 0) with close below prior bar
            if delta > 0 and current.close < prior.close:
                # Stop goes below the absorption bar's low
                stop_distance = current.high - current.close + self.stop_buffer
                
                if (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                    return AbsorptionSignal(
                        session=current.session,
                        timestamp=current.close_ts,
                        level=level,
                        direction="long",
                        absorption_bar_close=current.close,
                        absorption_bar_high=current.high,
                        absorption_bar_low=current.low,
                        delta_volume=delta,
                        stop_distance=stop_distance,
                    )

            # Short absorption: strong selling (delta < 0) with close above prior bar
            elif delta < 0 and current.close > prior.close:
                # Stop goes above the absorption bar's high
                stop_distance = current.close - current.low + self.stop_buffer
                
                if (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                    return AbsorptionSignal(
                        session=current.session,
                        timestamp=current.close_ts,
                        level=level,
                        direction="short",
                        absorption_bar_close=current.close,
                        absorption_bar_high=current.high,
                        absorption_bar_low=current.low,
                        delta_volume=abs(delta),
                        stop_distance=stop_distance,
                    )

        return None

    def _process_pending_setups(self, ctx, current) -> None:
        """Process pending setups: enter on absorption or wait for initiation."""
        if not self._pending_setups:
            return

        setup = self._pending_setups[0]

        # Enter on absorption alone immediately (no wait)
        if not self.require_initiation and setup.bars_since_detection >= 0:
            self._place_entry(ctx, setup, "absorption")
            self._pending_setups.popleft()
            return

        # If requiring initiation, wait for confirming bar
        if self.require_initiation and setup.bars_since_detection >= 1:
            if self._is_initiating_bar(current):
                self._place_entry(ctx, setup, "initiation")
                self._pending_setups.popleft()
                return

        # Expire old setups (after 6 bars without entry, expanded from 4)
        if setup.bars_since_detection > 6:
            self._pending_setups.popleft()

    def _is_initiating_bar(self, bar) -> bool:
        """
        Check if bar shows efficient movement confirming the absorption setup.
        
        Initiation requires:
        1. Efficiency above 60th percentile of recent bars
        2. Body size above 40% of recent average range
        """
        if len(self._recent_efficiencies) < 5 or len(self._recent_ranges) < 5:
            return False

        if bar.volume == 0 or bar.high == bar.low:
            return False

        # Current bar efficiency
        abs_delta = abs(bar.delta)
        if abs_delta == 0:
            return False

        current_efficiency = (bar.high - bar.low) / abs_delta

        # Compare to recent distribution (60th percentile, lowered from 65th)
        sorted_eff = sorted(self._recent_efficiencies)
        threshold_idx = max(0, int(len(sorted_eff) * 0.60))
        efficiency_threshold = sorted_eff[threshold_idx]

        # Check body size (lowered from 50% to 40%)
        body = abs(bar.close - bar.open)
        avg_range = sum(self._recent_ranges) / len(self._recent_ranges)

        return (
            current_efficiency > efficiency_threshold and
            body > avg_range * 0.4
        )

    def _place_entry(self, ctx, setup: AbsorptionSignal, entry_type: str) -> None:
        """Place entry with structural stop and target."""
        target = setup.stop_distance * self.target_multiple
        tag = f"{setup.direction}-{entry_type}"

        if setup.direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=setup.stop_distance,
                target_offset=target,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=setup.stop_distance,
                target_offset=target,
                tag=tag,
            )
