"""
Volume profile absorption strategy - test initiation requirement on top of absorption.

The campaign asks: "Does waiting for initiation improve the outcome?"

Analysis of trial history:
- #16 scored -0.1076 with 463 trades (pure absorption, no initiation)
- All subsequent attempts at pure absorption underperformed or didn't score
- The synthetic identical trades (70) across many parameter changes suggest we're
  matching baseline behavior when we don't add initiation

Key insight: The campaign explicitly states that initiation is OPTIONAL and the
comparison between absorption-only vs absorption+initiation is the point. We need
to test BOTH to answer the question.

Current approach: Test initiation requirement
1. **Detect absorption** - strong delta + wick + level proximity (same as #16)
2. **Wait for initiation** - next bar must show efficient move relative to flow
3. **Tag separately** - "absorption" vs "absorption-initiated" for comparison
4. **Track efficiency baseline** - rolling average of price movement per unit flow

Efficiency definition (from campaign guidance):
- For bars with net one-way flow: points moved / abs(signed_flow)
- For bars with balanced flow: skip (efficiency is meaningless)
- A bar initiates when: efficiency >> recent distribution AND body is large

This directly tests the campaign's second question: does waiting for initiation
help or hurt? By tagging both entry types, the run record will show which paid.

If synthetic produces identical trades, it means initiation never fires on synthetic
data (plausible - synthetic may not have the right flow patterns). If different,
we've separated from baseline and should see real results on actual data.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback_bars: int = 20,
        absorption_delta_ratio: float = 0.35,
        level_proximity: float = 0.5,
        min_wick_ratio: float = 0.25,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        efficiency_lookback: int = 10,
        efficiency_threshold: float = 1.5,
        min_body_ratio: float = 0.60,
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
        self.efficiency_lookback = efficiency_lookback
        self.efficiency_threshold = efficiency_threshold
        self.min_body_ratio = min_body_ratio
        self.size = size

        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        self._current_session: Optional[int] = None
        
        # Track absorption setups waiting for initiation
        self._pending_absorption: Optional[dict] = None
        self._pending_bar_ts: Optional[int] = None
        
        # Track efficiency metrics for initiation detection
        self._efficiency_history: deque = deque(maxlen=efficiency_lookback)

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
            self._pending_absorption = None
            self._pending_bar_ts = None
            self._efficiency_history.clear()

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_absorption = None
            self._pending_bar_ts = None
            return

        # Update efficiency history
        if len(history) >= 2:
            prev = history[-2]
            self._update_efficiency_history(current, prev)

        # Check if pending absorption has now initiated
        if self._pending_absorption and self._pending_bar_ts:
            if self._check_initiation(current):
                self._entered_bar_timestamps.add(self._pending_bar_ts)
                self._place_entry(ctx, self._pending_absorption, tag="absorption-initiated")
                self._pending_absorption = None
                self._pending_bar_ts = None
                return

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

        # Detect absorption: strong delta at high-volume level with wick
        absorption = self._detect_absorption(current, volume_profile)
        if absorption:
            # Store for initiation check on next bar
            self._pending_absorption = absorption
            self._pending_bar_ts = current.close_ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

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

    def _update_efficiency_history(self, current, prior) -> None:
        """Track efficiency: price movement per unit of signed flow."""
        if current.volume == 0:
            return

        # Only track bars with strong one-way flow
        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        if delta_ratio < 0.30:  # Balanced flow - efficiency is meaningless
            return

        # Calculate efficiency: points moved per unit of flow
        if delta > 0:  # Buying pressure - measure upward movement
            points_moved = current.close - prior.close
            if points_moved > 0:
                efficiency = points_moved / abs(delta)
                self._efficiency_history.append(efficiency)
        elif delta < 0:  # Selling pressure - measure downward movement
            points_moved = prior.close - current.close
            if points_moved > 0:
                efficiency = points_moved / abs(delta)
                self._efficiency_history.append(efficiency)

    def _check_initiation(self, current) -> bool:
        """
        Check if current bar shows initiation: efficient move with large body.
        
        Initiation requires:
        1. Strong one-way flow in current bar
        2. Efficiency well above recent baseline
        3. Body is large relative to recent range
        """
        if current.volume == 0 or current.high <= current.low:
            return False

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-way flow
        if delta_ratio < 0.30:
            return False

        # Calculate efficiency for this bar
        if delta > 0:  # Buying pressure
            points_moved = current.close - current.open
            if points_moved <= 0:
                return False
            efficiency = points_moved / abs(delta)
        else:  # Selling pressure
            points_moved = current.open - current.close
            if points_moved <= 0:
                return False
            efficiency = points_moved / abs(delta)

        # Check efficiency against baseline
        if not self._efficiency_history:
            return False

        avg_efficiency = sum(self._efficiency_history) / len(self._efficiency_history)
        if efficiency < avg_efficiency * self.efficiency_threshold:
            return False

        # Check body size relative to range
        body = abs(current.close - current.open)
        range_size = current.high - current.low
        if body / range_size < self.min_body_ratio:
            return False

        return True

    def _place_entry(self, ctx, setup: dict, tag: str = "absorption") -> None:
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
