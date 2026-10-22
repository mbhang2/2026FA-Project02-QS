"""
Order flow absorption at volume profile levels.

This strategy investigates whether absorption (flow that fails to move price) at
carried volume levels precedes a move away from those levels.

Key design:
1. Build a volume profile from 1s bars each session to capture price/volume nodes
2. Carry levels from prior sessions and current session
3. Detect absorption: large one-way flow at a level with price reversal
4. Wait for initiation: a confirming bar with high efficiency relative to flow
5. Enter with structural stops just beyond the absorption bar's extreme
6. Hold for moves between profile levels
7. Tag entries separately for absorption-only vs post-initiation

The structural stop removes fitting risk. The holding period targets multi-bar
moves between levels rather than scalping. Initiation is measured as bar
efficiency (points traveled per unit signed flow) relative to recent history.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

from src.core.types import Side


@dataclass
class VolumeNode:
    """A price level with accumulated volume."""
    price: float
    volume: int
    session: int


@dataclass
class AbsorptionSetup:
    """A detected absorption event ready for entry."""
    session: int
    bar_index: int
    level: float
    side: str  # "long" or "short"
    absorption_price: float
    absorption_bar_high: float
    absorption_bar_low: float
    absorption_volume: int
    flow_direction: int  # 1 for buy, -1 for sell
    stop_distance: float
    awaiting_initiation: bool = True


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        profile_interval: str = "1s",
        lookback_sessions: int = 2,
        absorption_threshold: float = 0.7,
        efficiency_percentile: float = 0.75,
        efficiency_window: int = 20,
        target_multiple: float = 2.0,
        max_stop_distance: float = 2.0,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.profile_interval = profile_interval
        self.lookback_sessions = lookback_sessions
        self.absorption_threshold = absorption_threshold
        self.efficiency_percentile = efficiency_percentile
        self.efficiency_window = efficiency_window
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.size = size

        # Session state
        self._current_session: Optional[int] = None
        self._session_profile: dict[float, int] = {}
        self._prior_levels: list[VolumeNode] = []
        self._recent_bars: deque = deque(maxlen=512)
        self._bar_efficiencies_up: deque = deque(maxlen=efficiency_window)
        self._bar_efficiencies_down: deque = deque(maxlen=efficiency_window)
        
        # Absorption tracking
        self._pending_setups: list[AbsorptionSetup] = []
        self._last_absorption_bar_index: int = -100

    def setup(self, ctx) -> None:
        ctx.subscribe(self.profile_interval)
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Handle session transitions
        if ctx.session != self._current_session:
            self._on_session_change(ctx)
            self._current_session = ctx.session

        # Update bar efficiency tracking from profile bars
        if self.profile_interval in ctx.bars:
            self._update_bar_efficiency(ctx)

        # Update session profile from profile-interval bars
        if self.profile_interval in ctx.bars:
            self._update_session_profile(ctx)

        # Check for absorption on main interval bars
        if self.interval in ctx.bars:
            self._check_for_absorption(ctx)
            self._process_pending_setups(ctx)

    def _on_session_change(self, ctx) -> None:
        """Carry levels forward when session changes."""
        if self._session_profile:
            # Extract top volume nodes from completed session
            sorted_levels = sorted(
                self._session_profile.items(),
                key=lambda x: x[1],
                reverse=True
            )
            self._prior_levels = [
                VolumeNode(price, vol, ctx.session - 1)
                for price, vol in sorted_levels[:5]
            ]
        self._session_profile = {}
        self._recent_bars.clear()
        self._pending_setups = []
        self._last_absorption_bar_index = -100

    def _update_session_profile(self, ctx) -> None:
        """Accumulate volume at price levels from 1s bars."""
        if self.profile_interval not in ctx.bars:
            return
        bar = ctx.bars[self.profile_interval]
        if bar.tick_count == 0:
            return

        # Simplified: accumulate at key price points
        for price in [bar.low, bar.high, (bar.high + bar.low) / 2]:
            rounded = round(price * 4) / 4  # Round to quarter point
            self._session_profile[rounded] = self._session_profile.get(rounded, 0) + bar.volume

    def _update_bar_efficiency(self, ctx) -> None:
        """Track efficiency (points per unit flow) for bars."""
        if self.profile_interval not in ctx.bars:
            return
        bar = ctx.bars[self.profile_interval]
        if bar.tick_count == 0:
            return

        delta = bar.delta
        range_traveled = bar.high - bar.low

        if range_traveled <= 0:
            return

        # Efficiency: range traveled per unit absolute flow
        abs_flow = abs(delta) if abs(delta) > 0 else 1
        efficiency = range_traveled / abs_flow

        if delta > 0:
            self._bar_efficiencies_up.append(efficiency)
        elif delta < 0:
            self._bar_efficiencies_down.append(efficiency)

    def _check_for_absorption(self, ctx) -> None:
        """Detect absorption at volume profile levels."""
        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        current = history[-1]
        prior = history[-2]
        current_bar_index = len(history) - 1

        # Prevent duplicate absorption detection on same bar
        if current_bar_index == self._last_absorption_bar_index:
            return

        # Get all relevant levels
        all_levels: list[float] = [node.price for node in self._prior_levels]
        all_levels.extend(self._session_profile.keys())

        if not all_levels:
            return

        for level in all_levels:
            # Check if current bar arrived at this level
            if not (min(current.low, prior.high) <= level <= max(current.high, prior.low)):
                continue

            # Detect absorption: large one-way flow with price reversal
            delta = current.delta
            if abs(delta) < current.volume * self.absorption_threshold:
                continue

            # Check for reversal (tail formation)
            if delta > 0:  # Buying pressure
                if current.close < prior.close:  # Price reversed down
                    self._pending_setups.append(
                        AbsorptionSetup(
                            session=ctx.session,
                            bar_index=current_bar_index,
                            level=level,
                            side="long",
                            absorption_price=current.close,
                            absorption_bar_high=current.high,
                            absorption_bar_low=current.low,
                            absorption_volume=delta,
                            flow_direction=1,
                            stop_distance=current.high - current.close + 0.25,
                            awaiting_initiation=True,
                        )
                    )
                    self._last_absorption_bar_index = current_bar_index
            else:  # Selling pressure
                if current.close > prior.close:  # Price reversed up
                    self._pending_setups.append(
                        AbsorptionSetup(
                            session=ctx.session,
                            bar_index=current_bar_index,
                            level=level,
                            side="short",
                            absorption_price=current.close,
                            absorption_bar_high=current.high,
                            absorption_bar_low=current.low,
                            absorption_volume=abs(delta),
                            flow_direction=-1,
                            stop_distance=current.close - current.low + 0.25,
                            awaiting_initiation=True,
                        )
                    )
                    self._last_absorption_bar_index = current_bar_index

    def _process_pending_setups(self, ctx) -> None:
        """Check pending setups for initiation or direct entry."""
        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        current = history[-1]

        for setup in self._pending_setups[:]:
            bars_since = len(history) - 1 - setup.bar_index

            if setup.awaiting_initiation and bars_since >= 1:
                # Check for initiating bar
                if self._is_initiating_bar(current, setup):
                    self._place_entry(ctx, setup, "post-initiation")
                    self._pending_setups.remove(setup)
            elif bars_since > 10:
                # Setup stale
                self._pending_setups.remove(setup)

    def _is_initiating_bar(self, bar, setup: AbsorptionSetup) -> bool:
        """Check if bar shows strong efficiency and body."""
        if len(self._bar_efficiencies_up) < 5 or len(self._bar_efficiencies_down) < 5:
            return False

        delta = bar.delta
        if abs(delta) < 10:
            return False

        range_traveled = bar.high - bar.low
        if range_traveled <= 0:
            return False

        efficiency = range_traveled / abs(delta)

        # Compare to recent distribution
        if delta > 0:
            recent = sorted(self._bar_efficiencies_up)
        else:
            recent = sorted(self._bar_efficiencies_down)

        threshold = recent[int(len(recent) * self.efficiency_percentile)]
        
        return efficiency > threshold

    def _place_entry(self, ctx, setup: AbsorptionSetup, tag_suffix: str) -> None:
        """Place entry order with structural stop."""
        if setup.stop_distance > self.max_stop_distance:
            return

        target = setup.stop_distance * self.target_multiple
        tag = f"{setup.side}-{tag_suffix}"

        if setup.side == "long":
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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
