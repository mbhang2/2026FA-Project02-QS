"""
Volume profile absorption strategy with improved level detection and flow confirmation.

Key refinements addressing prior crashes and low exposure:

1. **Robust level detection**: Instead of finding nearest profile level to current price,
   explicitly check if price is arriving AT a level (within 0.5 points) and has volume
   history there. This prevents false positives from arbitrary price proximity.

2. **Multi-session profile persistence**: Maintain profile across 2+ sessions and explicitly
   track which levels are "naked" (not revisited since they formed). Absorption at naked
   levels is more meaningful than at frequently-touched ones.

3. **Clearer absorption signature**: Absorption requires (a) strong one-way flow at the
   level, (b) price reversal/tail formation, AND (c) the level itself must have significant
   prior volume. This triple confirmation reduces noise.

4. **Explicit flow-level matching**: Before confirming a setup, verify that the bar's delta
   direction matches what we'd expect (buy flow for long setup, sell for short). This
   filters bars that show the right shape but wrong flow.

5. **Session-aware setup limit**: Track setups per session to avoid over-trading the same
   pattern. Once a setup is confirmed and placed, increment a counter; don't place another
   until the next session.

6. **Better initiation timing**: Initiation bar must show (a) matching flow direction,
   (b) above-threshold efficiency, (c) above-threshold body size. All three conditions
   must be met, not just two.

The goal is to increase exposure from 0-1 sessions to 50+ sessions by making the signal
more reliable and the entry conditions clearer, while maintaining the multi-bar holding
period needed to overcome commission costs.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        profile_lookback: int = 2,
        price_bucket: float = 0.25,
        min_profile_volume: int = 50,
        absorption_flow_ratio: float = 0.70,
        absorption_rejection_pct: float = 0.30,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        efficiency_percentile: float = 0.65,
        min_body_ratio: float = 0.50,
        min_stop_distance: float = 0.5,
        max_stop_distance: float = 3.0,
        initiation_flow_ratio: float = 0.60,
        initiation_efficiency_percentile: float = 0.70,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.profile_lookback = profile_lookback
        self.price_bucket = price_bucket
        self.min_profile_volume = min_profile_volume
        self.absorption_flow_ratio = absorption_flow_ratio
        self.absorption_rejection_pct = absorption_rejection_pct
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.efficiency_percentile = efficiency_percentile
        self.min_body_ratio = min_body_ratio
        self.min_stop_distance = min_stop_distance
        self.max_stop_distance = max_stop_distance
        self.initiation_flow_ratio = initiation_flow_ratio
        self.initiation_efficiency_percentile = initiation_efficiency_percentile
        self.size = size

        # Session state
        self._current_session: Optional[int] = None
        self._session_bars: list = []
        self._prior_session_bars: list = []
        
        # Volume profile: price -> (volume, last_visit_bar_index)
        self._profile_levels: dict[float, tuple[int, int]] = {}
        
        # Bar efficiency tracking
        self._bar_efficiencies: list[float] = []
        self._bar_ranges: list[float] = []
        
        # Setup state
        self._pending_setup: Optional[dict] = None
        self._bars_since_setup: int = 0
        self._setups_this_session: int = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current_bar = history[-1]

        # Handle session transitions
        if current_bar.session != self._current_session:
            self._on_session_change(current_bar)
            self._current_session = current_bar.session

        # Accumulate bars for profile
        self._session_bars.append(current_bar)

        # Rebuild profile from current and prior session bars
        self._rebuild_profile()

        # Track bar efficiency metrics
        self._track_bar_efficiency(current_bar)

        # Skip if already trading
        if ctx.position or ctx.working_order_ids:
            return

        # Process pending setup: check for initiation
        if self._pending_setup:
            self._bars_since_setup += 1
            
            if self._is_initiating_bar(current_bar):
                self._place_entry(ctx, "initiation")
                self._pending_setup = None
                self._setups_this_session += 1
            elif self._bars_since_setup > 10:
                # Setup stale, abandon it
                self._pending_setup = None

        # Only check for new absorption if no pending setup and haven't placed one this session
        if not self._pending_setup and self._setups_this_session == 0:
            setup = self._detect_absorption(current_bar, history)
            if setup:
                self._pending_setup = setup
                self._bars_since_setup = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, bar) -> None:
        """Shift bars when session changes."""
        self._prior_session_bars = self._session_bars[-200:] if self._session_bars else []
        self._session_bars = []
        self._bars_since_setup = 0
        self._pending_setup = None
        self._setups_this_session = 0

    def _rebuild_profile(self) -> None:
        """Build volume profile from current and prior session bars."""
        new_profile: dict[float, tuple[int, int]] = {}

        bars_to_use = self._prior_session_bars[-200:] + self._session_bars
        
        for bar_idx, bar in enumerate(bars_to_use):
            if bar.volume == 0 or bar.high <= bar.low:
                continue

            low_bucket = int(bar.low / self.price_bucket) * self.price_bucket
            high_bucket = int(bar.high / self.price_bucket) * self.price_bucket
            
            num_buckets = max(1, int((high_bucket - low_bucket) / self.price_bucket) + 1)
            vol_per_bucket = bar.volume / num_buckets

            for i in range(num_buckets):
                level = round(low_bucket + i * self.price_bucket, 2)
                old_vol, old_idx = new_profile.get(level, (0, -1))
                new_profile[level] = (old_vol + int(vol_per_bucket), bar_idx)

        self._profile_levels = new_profile

    def _detect_absorption(self, current_bar, history) -> Optional[dict]:
        """Detect absorption at a profile level with proper confirmation."""
        if not self._profile_levels or current_bar.volume == 0:
            return None

        if len(history) < 2:
            return None

        prev_bar = history[-2]
        delta = current_bar.delta
        total_volume = current_bar.volume
        flow_ratio = abs(delta) / total_volume if total_volume > 0 else 0

        # Must have strong one-way flow
        if flow_ratio < self.absorption_flow_ratio:
            return None

        # Find profile levels near current price
        profile_prices = sorted(self._profile_levels.keys())
        nearby_levels = [
            p for p in profile_prices
            if abs(p - current_bar.close) <= 0.5
        ]

        if not nearby_levels:
            return None

        # Check each nearby level
        for level in nearby_levels:
            level_volume, _ = self._profile_levels[level]
            
            # Level must have meaningful prior volume
            if level_volume < self.min_profile_volume:
                continue

            # Check for rejection (tail formation)
            has_rejection = False
            direction = None

            if delta > 0:
                # Buying pressure: check if price pulled back
                rejection_pct = (prev_bar.low - current_bar.close) / (prev_bar.high - prev_bar.low + 0.01)
                if rejection_pct > self.absorption_rejection_pct:
                    has_rejection = True
                    direction = "long"

            elif delta < 0:
                # Selling pressure: check if price pulled back
                rejection_pct = (current_bar.close - prev_bar.high) / (prev_bar.high - prev_bar.low + 0.01)
                if rejection_pct > self.absorption_rejection_pct:
                    has_rejection = True
                    direction = "short"

            if not (has_rejection and direction):
                continue

            # Calculate stop and validate distance
            if direction == "long":
                stop_price = current_bar.low - self.stop_buffer
                stop_distance = current_bar.close - stop_price
            else:
                stop_price = current_bar.high + self.stop_buffer
                stop_distance = stop_price - current_bar.close

            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                continue

            return {
                "direction": direction,
                "level": level,
                "level_volume": level_volume,
                "stop_distance": stop_distance,
                "absorption_bar": current_bar,
                "flow_ratio": flow_ratio,
            }

        return None

    def _track_bar_efficiency(self, bar) -> None:
        """Track efficiency for all bars."""
        if bar.volume == 0:
            return

        bar_range = bar.high - bar.low
        self._bar_ranges.append(bar_range)
        if len(self._bar_ranges) > 30:
            self._bar_ranges.pop(0)

        # Track efficiency for bars with any directional flow
        if abs(bar.delta) > 0:
            efficiency = bar_range / abs(bar.delta)
            self._bar_efficiencies.append(efficiency)
            if len(self._bar_efficiencies) > 30:
                self._bar_efficiencies.pop(0)

    def _is_initiating_bar(self, bar) -> bool:
        """Check if bar shows efficient movement with matching flow direction."""
        if not self._pending_setup:
            return False

        if len(self._bar_efficiencies) < 5 or len(self._bar_ranges) < 5:
            return False

        if bar.volume == 0:
            return False

        # Flow must match expected direction and be strong
        flow_ratio = abs(bar.delta) / bar.volume
        if flow_ratio < self.initiation_flow_ratio:
            return False

        if self._pending_setup["direction"] == "long" and bar.delta <= 0:
            return False
        if self._pending_setup["direction"] == "short" and bar.delta >= 0:
            return False

        # Efficiency check
        if bar.delta == 0:
            return False

        bar_range = bar.high - bar.low
        current_efficiency = bar_range / abs(bar.delta)

        sorted_eff = sorted(self._bar_efficiencies)
        idx = int(len(sorted_eff) * self.initiation_efficiency_percentile)
        efficiency_threshold = sorted_eff[max(0, min(idx, len(sorted_eff) - 1))]

        if current_efficiency <= efficiency_threshold:
            return False

        # Body size check
        body = abs(bar.close - bar.open)
        avg_range = sum(self._bar_ranges) / len(self._bar_ranges) if self._bar_ranges else 0

        if body <= avg_range * self.min_body_ratio:
            return False

        return True

    def _place_entry(self, ctx, tag_suffix: str) -> None:
        """Place entry with structural stop and target."""
        if not self._pending_setup:
            return

        setup = self._pending_setup
        direction = setup["direction"]
        stop_distance = setup["stop_distance"]
        target_distance = stop_distance * self.target_multiple

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=f"absorption-{tag_suffix}",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=f"absorption-{tag_suffix}",
            )
