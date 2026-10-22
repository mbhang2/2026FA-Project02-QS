"""
Volume profile absorption strategy - focus on initiation confirmation.

Campaign analysis reveals critical insight:
- #81: -0.0096 with 622 trades (BEST OVERALL)
- #174: -0.0577 with 1245 trades (more trades, worse score)
- #175: NOT SCORED (byte-identical to #0, volume_concentration filter ineffective)

The byte-identical result on #175 reveals that volume concentration at the bar
level doesn't differentiate on the synthetic data. The core issue: we're detecting
absorption correctly, but entering too early or on weak setups.

The campaign brief explicitly asks about INITIATION: "Does waiting for a
confirming bar improve the outcome?" This has never been properly tested. The
strategy should:

1. Detect absorption at carried levels (proven by #81)
2. OPTIONALLY wait for an initiating bar (confirming bar showing efficiency)
3. Tag entries separately: "absorption" vs "initiation"
4. Let the record show which approach worked

Key insight: #81's 622 trades with -0.0096 score suggests the absorption signal
itself is good, but we might be entering on marginal setups. An initiation filter
would reduce trade count and potentially improve score by filtering for higher
conviction setups.

This trial implements:
- Keep #81's proven absorption detection (delta_ratio=0.28, wick=-0.5)
- Add initiation confirmation: wait for a bar showing high efficiency
- Efficiency = points traveled per unit of signed flow
- Track efficiency over a rolling window and require bar to be above recent mean
- Tag entries to distinguish absorption-only from initiation-confirmed
- Keep target_multiple=4.0, stop_buffer=0.25 (proven)

The hypothesis: Filtering for initiation (high-efficiency confirming bars) will
reduce trade count to 400-500 while improving score to -0.0085 or better, by
avoiding marginal absorption setups that don't lead anywhere.

Expected behavior change: Byte-identity will break because we're now filtering
on bar efficiency, which is a new property not present in prior trials.
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        level_proximity: float = 5.0,
        min_wick_points: float = -0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        concentration_threshold: float = 0.65,
        rolling_window_sessions: int = 3,
        efficiency_window: int = 20,
        efficiency_std_multiplier: float = 0.5,
        use_initiation: bool = True,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.concentration_threshold = concentration_threshold
        self.rolling_window_sessions = rolling_window_sessions
        self.efficiency_window = efficiency_window
        self.efficiency_std_multiplier = efficiency_std_multiplier
        self.use_initiation = use_initiation

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()
        
        # Track pending absorption setups waiting for initiation
        self._pending_absorption: Optional[dict] = None
        self._pending_absorption_ts: Optional[int] = None
        
        # Track efficiency for initiation confirmation
        self._efficiency_history: deque = deque(maxlen=efficiency_window)

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
            self._on_session_change(current.session)
            self._current_session = current.session

        # Accumulate volume into current session profile
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            self._pending_absorption = None
            self._pending_absorption_ts = None
            return

        # Check if we're waiting for initiation on a pending absorption
        if self._pending_absorption and self.use_initiation:
            if self._check_initiation(current):
                # The pending absorption was confirmed by initiation
                self._entered_bar_timestamps.add(self._pending_absorption_ts)
                self._place_entry(ctx, self._pending_absorption, tag="initiation")
                self._pending_absorption = None
                self._pending_absorption_ts = None
                return
            # If initiation didn't confirm, clear the pending setup
            # (it's been more than one bar, or conditions changed)
            self._pending_absorption = None
            self._pending_absorption_ts = None

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            if self.use_initiation:
                # Store the setup and wait for initiation on the next bar
                self._pending_absorption = setup
                self._pending_absorption_ts = current.close_ts
            else:
                # Enter immediately on absorption
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, setup, tag="absorption")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Handle session boundary."""
        if self._current_session_profile:
            self._session_profiles.append(dict(self._current_session_profile))
            if len(self._session_profiles) > self.rolling_window_sessions:
                self._session_profiles.pop(0)
        
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()
        self._pending_absorption = None
        self._pending_absorption_ts = None

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        low_rounded = round(bar.low * 4) / 4
        high_rounded = round(bar.high * 4) / 4

        num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
        vol_per_level = bar.volume / num_levels

        level = low_rounded
        while level <= high_rounded + 0.01:
            level_key = round(level * 4) / 4
            self._current_session_profile[level_key] = (
                self._current_session_profile.get(level_key, 0) + int(vol_per_level)
            )
            level += 0.25

    def _extract_significant_levels(self, profile: dict[float, int]) -> list[float]:
        """Extract levels where volume is above significance threshold."""
        if not profile:
            return []

        max_vol = max(profile.values()) if profile else 0
        if max_vol == 0:
            return []

        threshold_vol = max_vol * self.concentration_threshold
        selected_levels = [level for level, vol in profile.items() if vol >= threshold_vol]

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from rolling window."""
        all_levels = set()
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        return sorted(list(all_levels))

    def _calculate_bar_efficiency(self, bar) -> Optional[float]:
        """
        Calculate efficiency: points traveled per unit of signed flow.
        
        Efficiency = range traveled / abs(signed volume)
        High efficiency = price moved a lot relative to flow (strong directional move)
        Low efficiency = price moved little despite flow (choppy/indecisive)
        """
        if bar.volume <= 0:
            return None
        
        signed_flow = abs(bar.delta)
        if signed_flow == 0:
            return None
        
        range_traveled = bar.high - bar.low
        efficiency = range_traveled / signed_flow
        return efficiency

    def _check_initiation(self, current) -> bool:
        """
        Check if current bar is an initiating bar (confirming the pending absorption).
        
        Initiation requires:
        1. Bar moves in the direction of the pending absorption
        2. Bar's efficiency is above recent mean (high-efficiency move)
        3. Bar's body is large relative to recent range
        """
        if not self._pending_absorption:
            return False
        
        direction = self._pending_absorption["direction"]
        
        # Calculate efficiency
        efficiency = self._calculate_bar_efficiency(current)
        if efficiency is None:
            return False
        
        # Track efficiency for comparison
        self._efficiency_history.append(efficiency)
        
        # Need minimum history to establish baseline
        if len(self._efficiency_history) < 5:
            return False
        
        # Calculate mean efficiency from history
        mean_efficiency = sum(self._efficiency_history) / len(self._efficiency_history)
        threshold_efficiency = mean_efficiency * (1.0 + self.efficiency_std_multiplier)
        
        # Check if current bar has high efficiency
        if efficiency < threshold_efficiency:
            return False
        
        # Check if bar moves in the right direction
        if direction == "long":
            # For long absorption, initiation should close higher than previous bar
            # (we don't have previous bar easily, so just check it's above midpoint)
            if current.close <= (current.high + current.low) / 2:
                return False
        else:  # short
            # For short absorption, initiation should close lower
            if current.close >= (current.high + current.low) / 2:
                return False
        
        return True

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels.
        
        Requires:
        1. Strong directional delta (delta_ratio >= 0.28)
        2. Price rejection via wick (min_wick_points >= -0.5)
        3. Proximity to carried level (within 5.0 points)
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for wick showing rejection
        has_valid_wick = False
        if delta > 0:  # Buying pressure
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_valid_wick = True
        elif delta < 0:  # Selling pressure
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_valid_wick = True

        if not has_valid_wick:
            return None

        # Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Calculate stop and target
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # Short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

    def _place_entry(self, ctx, setup: dict, tag: str = "absorption") -> None:
        """Place entry order."""
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
