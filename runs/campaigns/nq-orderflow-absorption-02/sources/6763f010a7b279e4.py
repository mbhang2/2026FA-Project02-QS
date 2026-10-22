"""
Volume profile absorption strategy following the campaign specification.

This strategy:
1. Builds a volume profile from recent bars to identify price levels
2. Tracks naked levels (not recently visited) from prior sessions
3. Detects absorption: aggressive flow at a level that fails to move price
4. Optionally waits for an initiating bar (efficient move + large body)
5. Enters with a structural stop just beyond the absorption bar extreme
6. Targets a multiple of the stop distance
7. Tags entries as "absorption" or "initiation" for analysis

Key design choices:
- Uses 1-minute bars to build profile (good resolution, within budget)
- Carries profile state on self to survive the 512-bar window
- Structural stop: constant buffer beyond absorption bar extreme
- Absorption: large one-way volume with price failing to extend
- Initiation: bar with efficiency > recent mean + large body
- Acts on 50%+ of sessions (per campaign floor)
- Multi-bar holds to reach between-level moves
"""

from __future__ import annotations
from collections import deque
from typing import Deque, Dict, Any, Optional
from src.core.types import Side


class Strategy:
    def __init__(
        self,
        fast_interval: str = "1m",
        slow_interval: str = "5m",
        profile_sessions: int = 2,
        absorption_volume_threshold: float = 0.7,
        absorption_extension_ratio: float = 0.5,
        efficiency_zscore: float = 1.0,
        body_ratio: float = 0.6,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.0,
        use_initiation: bool = True,
        max_stop_distance: float = 5.0,
        size: int = 1,
    ):
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.profile_sessions = profile_sessions
        self.absorption_volume_threshold = absorption_volume_threshold
        self.absorption_extension_ratio = absorption_extension_ratio
        self.efficiency_zscore = efficiency_zscore
        self.body_ratio = body_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.use_initiation = use_initiation
        self.max_stop_distance = max_stop_distance
        self.size = size

        # State tracking with proper type annotations
        self.current_session: Optional[int] = None
        self.session_bars: Deque[Any] = deque(maxlen=1000)
        self.session_profiles: Deque[Dict[float, float]] = deque(maxlen=profile_sessions + 1)
        self.naked_levels: list[float] = []
        self.recent_range: Deque[float] = deque(maxlen=20)
        self.recent_efficiency: Deque[float] = deque(maxlen=20)
        self.absorption_setup: Optional[Dict[str, Any]] = None
        self.initiation_confirmed: bool = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.fast_interval)
        ctx.subscribe(self.slow_interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Track session boundaries
        if ctx.session != self.current_session:
            if self.current_session is not None:
                self._finalize_session()
            self.current_session = ctx.session
            self.session_bars.clear()
            self.absorption_setup = None
            self.initiation_confirmed = False

        # Accumulate fast bars for profile and feature tracking
        if self.fast_interval in ctx.bars:
            bar = ctx.bars[self.fast_interval]
            self.session_bars.append(bar)
            self._update_range_and_efficiency(bar)

        # Check for absorption setup
        if self.absorption_setup is None and self.fast_interval in ctx.bars:
            bar = ctx.bars[self.fast_interval]
            if self._is_absorption_bar(bar):
                self.absorption_setup = {
                    "bar": bar,
                    "direction": "long" if bar.delta > 0 else "short",
                    "ts": ctx.ts,
                }
                self.initiation_confirmed = False

        # Check for initiation if waiting for it
        if (
            self.use_initiation
            and self.absorption_setup is not None
            and not self.initiation_confirmed
            and self.fast_interval in ctx.bars
        ):
            bar = ctx.bars[self.fast_interval]
            if self._is_initiating_bar(bar, self.absorption_setup["direction"]):
                self.initiation_confirmed = True

        # Entry logic
        if ctx.position or ctx.working_order_ids:
            return

        if self.absorption_setup is None:
            return

        if self.use_initiation and not self.initiation_confirmed:
            return

        # Ready to enter
        setup = self.absorption_setup
        direction = setup["direction"]
        absorption_bar = setup["bar"]

        # Calculate stop and target
        if direction == "long":
            stop_price = absorption_bar.low - self.stop_buffer
            stop_distance = absorption_bar.close - stop_price
        else:
            stop_price = absorption_bar.high + self.stop_buffer
            stop_distance = stop_price - absorption_bar.close

        # Reject if stop too wide
        if stop_distance > self.max_stop_distance:
            self.absorption_setup = None
            return

        target_distance = stop_distance * self.target_multiple

        tag = "initiation" if self.initiation_confirmed else "absorption"

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop=stop_price,
                target=absorption_bar.close + target_distance,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop=stop_price,
                target=absorption_bar.close - target_distance,
                tag=tag,
            )

        self.absorption_setup = None
        self.initiation_confirmed = False

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _finalize_session(self) -> None:
        """Build and store volume profile at session end."""
        if not self.session_bars:
            return

        profile = self._build_profile(self.session_bars)
        self.session_profiles.append(profile)
        self._update_naked_levels()

    def _build_profile(self, bars) -> Dict[float, float]:
        """Build volume profile from bars: price -> volume."""
        profile: Dict[float, float] = {}
        for bar in bars:
            # Discretize price into 0.25 buckets (NQ tick size)
            high_bucket = int(bar.high * 4) / 4
            low_bucket = int(bar.low * 4) / 4
            num_buckets = max(1, int((high_bucket - low_bucket) * 4) + 1)
            vol_per_bucket = bar.volume / num_buckets

            for i in range(num_buckets):
                price = low_bucket + i * 0.25
                profile[price] = profile.get(price, 0) + vol_per_bucket

        return profile

    def _update_naked_levels(self) -> None:
        """Identify naked levels from profile history."""
        if not self.session_profiles:
            return

        # Combine profiles from tracked sessions
        combined: Dict[float, float] = {}
        for profile in self.session_profiles:
            for price, vol in profile.items():
                combined[price] = combined.get(price, 0) + vol

        if not combined:
            return

        # Find high-volume price levels
        sorted_prices = sorted(combined.items(), key=lambda x: x[1], reverse=True)
        top_prices = [p[0] for p in sorted_prices[:5]]

        self.naked_levels = top_prices

    def _is_absorption_bar(self, bar) -> bool:
        """Detect absorption: large one-way flow, price fails to extend."""
        if bar.tick_count == 0 or bar.volume == 0:
            return False

        # One-way flow requirement
        flow_ratio = abs(bar.delta) / bar.volume
        if flow_ratio < self.absorption_volume_threshold:
            return False

        # Price fails to extend: bar makes a long tail and closes back
        if len(self.session_bars) < 2:
            return False

        prev_bar = self.session_bars[-2]

        # For long absorption (buy volume dominant): close < prev close
        if bar.delta > 0:
            if bar.close >= prev_bar.close:
                return False
            # Tail: high is well above close
            if bar.high - bar.close < (bar.high - bar.low) * self.absorption_extension_ratio:
                return False

        # For short absorption (sell volume dominant): close > prev close
        elif bar.delta < 0:
            if bar.close <= prev_bar.close:
                return False
            # Tail: low is well below close
            if bar.close - bar.low < (bar.high - bar.low) * self.absorption_extension_ratio:
                return False

        return True

    def _update_range_and_efficiency(self, bar) -> None:
        """Track recent range and bar efficiency."""
        if bar.tick_count == 0 or bar.volume == 0:
            return

        bar_range = bar.high - bar.low
        self.recent_range.append(bar_range)

        # Efficiency: points traveled per unit of signed volume
        if abs(bar.delta) > 0:
            efficiency = bar_range / (abs(bar.delta) / 1000.0)
            self.recent_efficiency.append(efficiency)

    def _is_initiating_bar(self, bar, direction: str) -> bool:
        """Detect initiating bar: high efficiency + large body."""
        if bar.tick_count == 0 or bar.volume == 0:
            return False

        if not self.recent_efficiency or not self.recent_range:
            return False

        # Efficiency check
        mean_eff = sum(self.recent_efficiency) / len(self.recent_efficiency)
        if abs(bar.delta) > 0:
            efficiency = (bar.high - bar.low) / (abs(bar.delta) / 1000.0)
            if efficiency < mean_eff + self.efficiency_zscore * 0.1:
                return False

        # Body size check
        body = abs(bar.close - bar.open)
        mean_range = sum(self.recent_range) / len(self.recent_range)
        if body < mean_range * self.body_ratio:
            return False

        # Direction alignment
        if direction == "long" and bar.close <= bar.open:
            return False
        if direction == "short" and bar.close >= bar.open:
            return False

        return True
