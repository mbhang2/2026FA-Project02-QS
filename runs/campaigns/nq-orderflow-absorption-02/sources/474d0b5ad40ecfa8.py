"""
Volume profile absorption strategy - test dual-path confirmation with asymmetric filters.

The synthetic data produces byte-identical trades because the current filtering
(level_proximity=3.5, volume weighting) doesn't discriminate in that constrained
dataset. We need a change that actually affects entry decisions.

Key observation from trial history:
- #81: -0.0096 with 622 trades (BEST baseline)
- #165: -0.0350 with 658 trades
- #155: -0.0462 with 859 trades
- #195: +0.0646 (losing) with confirmation delay

The confirmation delay in #195 actually hurt performance (+0.0646 is worse than
-0.0096), suggesting waiting for the next bar filters out winning trades.

This trial tests a different hypothesis: the issue is not when we enter, but
HOW we identify levels. The current volume weighting (high/low/close split)
may not match where real support/resistance forms.

Changes:
1. Revert to simpler level accumulation: distribute volume evenly across the
   bar's range (0.25-point granularity). This was proven in #81.

2. Add a DUAL-PATH entry system:
   - Path A (Immediate): Enter on absorption if within 4.0 points of ANY carried level
   - Path B (Confirmed): Enter on absorption if within 5.5 points AND next bar confirms
   
   This tests both hypotheses:
   - Does immediate entry on tight proximity work? (Path A)
   - Does waiting for confirmation help at looser proximity? (Path B)
   
   Tag them separately so we can measure which path actually works.

3. Keep both paths enabled so we can see in the record which one wins, rather
   than choosing one and losing the comparison data.

4. Use min_absolute_delta filter to ensure genuine absorption, not just ratio.

The synthetic data will produce different trades because:
- Path A enters immediately at 4.0 proximity
- Path B waits for confirmation at 5.5 proximity
- These are different entry conditions that will trigger on different bars

Expected: 700-1000 total trades (both paths combined), with clear separation
between immediate and confirmed entries in the tag. This lets us measure which
approach actually works without having to run two separate strategies.
"""

from __future__ import annotations

from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.28,
        min_absolute_delta: int = 50,
        level_proximity_immediate: float = 4.0,
        level_proximity_confirm: float = 5.5,
        min_wick_points: float = -0.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        rolling_window_sessions: int = 2,
        concentration_threshold: float = 0.75,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_absolute_delta = min_absolute_delta
        self.level_proximity_immediate = level_proximity_immediate
        self.level_proximity_confirm = level_proximity_confirm
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.rolling_window_sessions = rolling_window_sessions
        self.concentration_threshold = concentration_threshold

        # Rolling window of session profiles
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = -1
        self._current_session_profile: dict[float, int] = {}
        
        # Track pending absorption setups waiting for confirmation (Path B)
        self._pending_setup: Optional[dict] = None
        self._pending_bar_ts: Optional[int] = None
        
        # Track bars we've entered on to avoid re-entry
        self._entered_bar_timestamps: set = set()

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

        # Skip if already in position or have working orders
        if ctx.position or ctx.working_order_ids:
            self._pending_setup = None
            self._pending_bar_ts = None
            return

        # Check if we should confirm and enter a pending setup (Path B)
        if self._pending_setup and self._pending_bar_ts:
            if self._should_confirm_setup(current, self._pending_setup):
                if current.close_ts not in self._entered_bar_timestamps:
                    self._entered_bar_timestamps.add(current.close_ts)
                    self._place_entry(ctx, self._pending_setup, tag="confirmed")
            # Clear pending setup after attempting confirmation
            self._pending_setup = None
            self._pending_bar_ts = None
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect absorption at carried levels
        setup = self._detect_absorption(current)
        if setup:
            # Path A: Enter immediately if within tight proximity
            if setup.get("path") == "immediate":
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, setup, tag="immediate")
            
            # Path B: Queue for confirmation if within looser proximity
            elif setup.get("path") == "confirm":
                self._pending_setup = setup
                self._pending_bar_ts = current.close_ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, new_session: int) -> None:
        """Handle session boundary - add completed session to rolling window."""
        if self._current_session_profile:
            self._session_profiles.append(dict(self._current_session_profile))
            # Keep only last N sessions in rolling window
            if len(self._session_profiles) > self.rolling_window_sessions:
                self._session_profiles.pop(0)
        
        # Reset for new session
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()
        self._pending_setup = None
        self._pending_bar_ts = None

    def _update_session_profile(self, bar) -> None:
        """
        Accumulate volume at price levels for current session.
        
        Simple even distribution across the bar's range at 0.25-point granularity.
        This was proven effective in #81.
        """
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Use 0.25-point granularity for level accumulation
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
        """Extract levels representing the top concentration_threshold of volume."""
        if not profile:
            return []

        sorted_items = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)

        if total_vol == 0:
            return []

        # Accumulate from highest volume levels until we reach the threshold
        cumulative_vol = 0
        selected_levels = []
        
        for level, vol in sorted_items:
            selected_levels.append(level)
            cumulative_vol += vol
            if cumulative_vol >= total_vol * self.concentration_threshold:
                break

        return sorted(selected_levels)

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from the rolling window of sessions."""
        all_levels = set()
        
        # Add levels from current session
        all_levels.update(self._extract_significant_levels(self._current_session_profile))
        
        # Add levels from all prior sessions in the rolling window
        for session_profile in self._session_profiles:
            all_levels.update(self._extract_significant_levels(session_profile))
        
        return sorted(list(all_levels))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption at carried levels with dual-path entry logic.
        
        Path A (Immediate): tight proximity (4.0) - enter right away
        Path B (Confirmed): loose proximity (5.5) - queue for confirmation
        
        This tests both hypotheses without choosing between them.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume if current.volume > 0 else 0

        # Filter 1: Strong directional delta ratio
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Filter 2: Absolute delta must be significant
        if abs(delta) < self.min_absolute_delta:
            return None

        # Filter 3: Evidence of price rejection via wick
        has_valid_wick = False
        if delta > 0:  # Buying pressure - check lower wick
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_valid_wick = True
        elif delta < 0:  # Selling pressure - check upper wick
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_valid_wick = True

        if not has_valid_wick:
            return None

        # Filter 4: Check proximity to carried level and determine entry path
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)

        # Determine which path this setup qualifies for
        entry_path = None
        if min_distance <= self.level_proximity_immediate:
            entry_path = "immediate"
        elif min_distance <= self.level_proximity_confirm:
            entry_path = "confirm"
        else:
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
                "absorption_close": current.close,
                "path": entry_path,
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
                "absorption_close": current.close,
                "path": entry_path,
            }

    def _should_confirm_setup(self, confirming_bar, setup: dict) -> bool:
        """
        Check if the confirming bar validates the absorption setup (Path B).
        
        For long (buying pressure): confirming bar should close above absorption close
        For short (selling pressure): confirming bar should close below absorption close
        """
        if setup["direction"] == "long":
            return confirming_bar.close > setup["absorption_close"]
        else:  # short
            return confirming_bar.close < setup["absorption_close"]

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
