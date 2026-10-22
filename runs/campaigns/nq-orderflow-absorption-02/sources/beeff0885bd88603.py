"""
Volume profile absorption strategy - test initiation as a FILTER, not a parallel path.

The core insight from trial history:
- #16 scored -0.1076 with 463 trades (pure absorption, best so far)
- All recent trials with dual-entry produce IDENTICAL synthetic trades
- The synthetic identical behavior means the initiation logic isn't changing order generation

The problem with the previous approach: queuing absorptions for later initiation check
doesn't change WHICH bars generate orders on synthetic data - it just delays them.
The synthetic baseline (70 trades) stays identical because the same bars still trigger.

New approach: Make initiation a GATE that prevents entry, not a queue that delays it.
- If `use_initiation_filter=True`: only enter if the SAME bar shows both absorption AND initiation
- If `use_initiation_filter=False`: enter on absorption alone
- This way, different parameter values actually change the set of bars that generate orders

Key change: Detect both absorption AND initiation on the SAME bar, then decide to enter.
This will produce DIFFERENT synthetic trades if initiation truly filters setups.

The hypothesis: absorption at a level precedes a move. Initiation is a confirming bar.
If we require BOTH on the same bar, we're being more selective. If that changes synthetic
trades, it means the filter works. If synthetic stays identical, initiation isn't real.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional
import math


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback_bars: int = 20,
        absorption_delta_ratio: float = 0.40,
        level_proximity: float = 0.75,
        min_wick_ratio: float = 0.15,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        efficiency_window: int = 10,
        efficiency_threshold_stddev: float = 1.0,
        use_initiation_filter: bool = False,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback_bars = lookback_bars
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.efficiency_window = efficiency_window
        self.efficiency_threshold_stddev = efficiency_threshold_stddev
        self.use_initiation_filter = use_initiation_filter
        self.size = size

        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        self._efficiency_history: list = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current = history[-1]

        # Handle session boundary
        if current.session != self._current_session:
            self._current_session = current.session
            self._entered_bar_timestamps = set()

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Track efficiency for initiation detection
        if len(history) >= 2:
            self._update_efficiency_history(current)

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Build volume profile from recent bars
        recent_bars = history.last(min(self.lookback_bars, len(history)))
        if not recent_bars:
            return

        volume_profile = self._build_volume_profile(recent_bars)
        if not volume_profile:
            return

        # Detect absorption
        absorption = self._detect_absorption(current, volume_profile)
        if not absorption:
            return

        # Decide whether to enter based on initiation filter
        should_enter = False
        entry_tag = "absorption"

        if self.use_initiation_filter:
            # Require BOTH absorption AND initiation on same bar
            threshold = self._get_efficiency_threshold()
            if threshold is not None:
                if self._is_initiating_bar(current, threshold):
                    should_enter = True
                    entry_tag = "absorption-initiated"
        else:
            # Enter on absorption alone
            should_enter = True
            entry_tag = "absorption-immediate"

        if should_enter:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption, tag=entry_tag)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _update_efficiency_history(self, bar) -> None:
        """Track bar efficiency: range moved per unit of flow."""
        if bar.volume == 0 or bar.delta == 0:
            return

        range_traveled = bar.high - bar.low
        flow_magnitude = abs(bar.delta)

        if flow_magnitude > 0:
            efficiency = range_traveled / flow_magnitude
            self._efficiency_history.append(efficiency)
            # Keep only recent history
            if len(self._efficiency_history) > self.efficiency_window:
                self._efficiency_history.pop(0)

    def _get_efficiency_threshold(self) -> Optional[float]:
        """Get efficiency threshold: median + N * stddev of recent bars."""
        if len(self._efficiency_history) < 5:
            return None

        efficiencies = sorted(self._efficiency_history)
        median = efficiencies[len(efficiencies) // 2]

        mean = sum(efficiencies) / len(efficiencies)
        variance = sum((x - mean) ** 2 for x in efficiencies) / len(efficiencies)
        stddev = math.sqrt(variance) if variance > 0 else 0.0

        threshold = median + self.efficiency_threshold_stddev * stddev
        return threshold

    def _is_initiating_bar(self, bar, efficiency_threshold: float) -> bool:
        """Check if bar shows initiating characteristics: high efficiency."""
        if bar.volume == 0 or bar.delta == 0:
            return False

        range_traveled = bar.high - bar.low
        flow_magnitude = abs(bar.delta)

        if flow_magnitude == 0:
            return False

        efficiency = range_traveled / flow_magnitude
        return efficiency > efficiency_threshold

    def _build_volume_profile(self, bars) -> dict[float, int]:
        """Build volume profile from recent bars with 0.25-point granularity."""
        profile: dict[float, int] = defaultdict(int)

        for bar in bars:
            if bar.volume <= 0 or bar.high <= bar.low:
                continue

            low_rounded = round(bar.low * 4) / 4
            high_rounded = round(bar.high * 4) / 4

            num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
            vol_per_level = bar.volume / num_levels

            level = low_rounded
            while level <= high_rounded + 0.01:
                level_key = round(level * 4) / 4
                profile[level_key] += int(vol_per_level)
                level += 0.25

        return dict(profile)

    def _get_high_volume_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract high-volume levels from profile (top 50% of volume)."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.50:
                break

        return sorted(list(set(top_levels)))

    def _detect_absorption(
        self, current, volume_profile: dict[float, int]
    ) -> Optional[dict]:
        """
        Detect absorption: strong one-sided flow at a high-volume level with failed extension.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Marker 1: Strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low
        if range_size <= 0:
            return None

        # Marker 2: Wick opposite to flow direction
        has_significant_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick / range_size >= self.min_wick_ratio:
                has_significant_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick / range_size >= self.min_wick_ratio:
                has_significant_wick = True

        if not has_significant_wick:
            return None

        # Marker 3: Price near a high-volume level
        high_vol_levels = self._get_high_volume_levels(volume_profile)
        if not high_vol_levels:
            return None

        min_distance = min(abs(current.close - level) for level in high_vol_levels)
        if min_distance > self.level_proximity:
            return None

        # Calculate stop and target
        if delta > 0:  # Long absorption
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        else:  # Short absorption
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None

            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

    def _place_entry(self, ctx, setup: dict, tag: str) -> None:
        """Place entry order on absorption detection."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag=tag,
            )
