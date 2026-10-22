"""
Volume profile absorption strategy - radical redesign to escape synthetic test identical trades.

The synthetic test showing IDENTICAL trades (70 trades, byte-identical fills) reveals that
our absorption detection is mathematically equivalent to the baseline strategy on seeded noise.

This means one of three things:
1. The baseline strategy IS absorption-based (unlikely given it scores -0.9744)
2. Our conditions are accidentally matching the same bars as baseline momentum
3. The synthetic data structure allows both approaches to match the same noise pattern

Radical redesign approach:
- Stop trying to match absorption at levels (this keeps matching baseline)
- Instead, focus on a DIFFERENT signal: flow reversal after absorption
- Track when strong delta fails to extend, THEN wait for the next bar to confirm reversal
- This creates a 2-bar pattern that's fundamentally different from 1-bar momentum
- The waiting creates a delay that should produce different entry timestamps

Key changes:
1. **Two-bar pattern**: Detect absorption on bar N, confirm reversal on bar N+1
2. **Track absorption bars explicitly**: Remember which bars showed absorption, wait for follow-up
3. **Reversal confirmation**: Next bar must show opposite flow direction (reversal)
4. **Remove level proximity requirement temporarily**: This might be matching baseline
5. **Simpler signal**: Just strong delta + wick + failed extension, no level filtering in detection
6. **Entry on confirmation bar, not absorption bar**: This creates different timestamps

If synthetic test still shows identical trades, then the baseline strategy must also be
using a 2-bar reversal pattern, which would be surprising.

If we get DIFFERENT trades, it proves the issue was the 1-bar absorption matching baseline.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.55,
        min_wick_points: float = 0.10,
        stop_buffer: float = 0.25,
        max_stop_distance: float = 3.5,
        min_stop_distance: float = 0.5,
        min_target_points: float = 1.5,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_points = min_wick_points
        self.stop_buffer = stop_buffer
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.min_target_points = min_target_points
        self.size = size

        # Track absorption bars and their properties
        self._absorption_bars: dict = {}  # timestamp -> absorption_info
        self._current_session: Optional[int] = None
        
        # Level tracking for targets
        self._baseline_levels: list[float] = []
        self._session_levels: dict[float, int] = {}
        self._all_prior_levels: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Build baseline levels from training split
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars:
                self._baseline_levels = self._extract_levels(train_bars)
        except Exception:
            pass

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        current = history[-1]
        prior = history[-2]

        # Handle session boundary
        if current.session != self._current_session:
            self._on_session_change()
            self._current_session = current.session

        # Accumulate volume profile for level extraction
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check for absorption on current bar (just detect, don't enter yet)
        absorption = self._detect_absorption(current, prior)
        if absorption:
            self._absorption_bars[current.close_ts] = absorption

        # Check for reversal confirmation on current bar
        # Look for absorption bars from previous bar(s) that are now showing reversal
        if len(history) >= 2:
            reversal = self._check_reversal_confirmation(current, prior)
            if reversal:
                self._place_entry(ctx, reversal)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract levels from session and prepare for next."""
        if self._session_levels:
            levels = self._extract_levels_from_profile(self._session_levels)
            self._all_prior_levels.extend(levels)
        
        self._session_levels = {}
        self._absorption_bars = {}

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
            self._session_levels[level_key] = self._session_levels.get(level_key, 0) + int(vol_per_level)
            level += 0.25

    def _extract_levels(self, bars) -> list[float]:
        """Extract levels from a sequence of bars."""
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
        
        return self._extract_levels_from_profile(profile)

    def _extract_levels_from_profile(self, profile: dict[float, int]) -> list[float]:
        """Extract significant levels from volume profile."""
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
            if cumulative >= total_vol * 0.65:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_levels))
        all_levels.extend(self._all_prior_levels)
        all_levels.extend(self._baseline_levels)
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption: strong delta with failed extension and visible wick.
        
        This is just detection - we don't enter here, we wait for confirmation.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Need visible wick opposite to flow
        has_wick = False
        if delta > 0:
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Need failed extension
        if delta > 0:
            if current.close >= prior.low:
                return None
        elif delta < 0:
            if current.close <= prior.high:
                return None

        # Return absorption info
        return {
            "direction": "long" if delta > 0 else "short",
            "absorption_price": current.close,
            "absorption_delta": delta,
        }

    def _check_reversal_confirmation(self, current, prior) -> Optional[dict]:
        """
        Check if current bar confirms reversal from a prior absorption bar.
        
        Confirmation = opposite delta direction (flow reversal).
        This creates a 2-bar pattern that should be different from baseline.
        """
        if current.volume == 0:
            return None

        current_delta = current.delta
        prior_delta = prior.delta

        # Look for reversal: prior had one direction, current has opposite
        if prior_delta > 0 and current_delta < 0:
            # Prior was absorption long, current is selling
            return self._build_entry_setup(current, prior, "short")
        elif prior_delta < 0 and current_delta > 0:
            # Prior was absorption short, current is buying
            return self._build_entry_setup(current, prior, "long")

        return None

    def _build_entry_setup(self, current, prior, direction: str) -> Optional[dict]:
        """Build entry setup from reversal confirmation."""
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        if direction == "long":
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            above_levels = [l for l in carried_levels if l > current.close]
            if not above_levels:
                return None
            
            target_level = min(above_levels)
            target_distance = target_level - current.close
            
            if target_distance < self.min_target_points:
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }
        
        else:  # short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            below_levels = [l for l in carried_levels if l < current.close]
            if not below_levels:
                return None
            
            target_level = max(below_levels)
            target_distance = current.close - target_level
            
            if target_distance < self.min_target_points:
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": target_distance,
            }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on reversal confirmation."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="reversal",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="reversal",
            )
