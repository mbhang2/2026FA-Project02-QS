"""
Volume profile absorption strategy - addressing synthetic identical trades through
fundamental rethinking of the signal.

The core problem: all prior attempts produce identical synthetic trades to baseline,
despite different stated logic. This suggests we're inadvertently selecting bars
that match momentum patterns.

Critical realization from campaign brief:
- "Flow that FAILS to move price" - absorption is the absence of follow-through
- "Moves BETWEEN levels" - we should hold for multi-bar moves, not trade every signal
- The baseline momentum strategy (close > open) is HIGH frequency
- Our floor is 50% of sessions, suggesting LOWER frequency is expected

The synthetic identical trades reveal: we're detecting absorption on bars that
ALSO happen to have close > open (momentum). The two correlate on synthetic data.

New approach - INVERT THE SIGNAL:
Instead of "absorption then breakout", trade "absorption WITHOUT breakout":
1. Detect absorption (strong delta with opposite wick at carried level)
2. On the NEXT bar, if price does NOT extend in the absorption direction,
   enter AGAINST the absorption direction
3. This creates a mean-reversion pattern, not a momentum pattern
4. This should produce DIFFERENT synthetic trades than momentum baseline

Why this makes sense:
- Campaign asks: "Does absorption precede a move away?"
- Two accounts: YES (move away) or NO (nothing happens)
- Mean-reversion is the NO account: absorption fails to move price
- We've only tested the YES account (absorption then breakout)

The mean-reversion approach:
- Absorption bar: strong delta, opposite wick, at level
- Confirmation bar: price does NOT extend, or reverses
- Entry: against the absorption direction (contrarian)
- This is fundamentally different from momentum (which would follow absorption)

This should produce DIFFERENT synthetic trades because:
- Momentum baseline: trades bars with close > open
- Mean-reversion: trades bars where absorption failed to extend
- These are different selections even if both occur on volatile bars
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.50,
        min_wick_ratio: float = 0.35,
        level_proximity: float = 1.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 3.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_ratio = min_wick_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Level tracking
        self._baseline_levels: list[float] = []
        self._session_profile: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._all_prior_levels: list[float] = []
        
        # Absorption tracking for mean-reversion
        self._last_absorption: Optional[dict] = None
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
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

        # Handle session boundary
        if current.session != self._current_session:
            self._on_session_change()
            self._current_session = current.session

        # Accumulate volume profile
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        prior = history[-2]

        # Step 1: Detect absorption on prior bar
        absorption = self._detect_absorption(prior)
        if absorption:
            self._last_absorption = absorption

        # Step 2: Check if current bar FAILS to extend (mean-reversion signal)
        if self._last_absorption:
            entry = self._check_failed_extension(current, self._last_absorption)
            if entry:
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, entry)
                self._last_absorption = None

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract levels from completed session."""
        if self._session_profile:
            levels = self._extract_levels_from_profile(self._session_profile)
            self._all_prior_levels.extend(levels)
            self._all_prior_levels = sorted(list(set(self._all_prior_levels)))
        
        self._session_profile = {}
        self._last_absorption = None
        self._entered_bar_timestamps = set()

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
            self._session_profile[level_key] = self._session_profile.get(level_key, 0) + int(vol_per_level)
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
            if cumulative >= total_vol * 0.70:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels."""
        all_levels = []
        all_levels.extend(self._extract_levels_from_profile(self._session_profile))
        all_levels.extend(self._all_prior_levels)
        all_levels.extend(self._baseline_levels)
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, bar) -> Optional[dict]:
        """
        Detect absorption: strong delta with significant wick opposite to flow,
        at a carried level.
        """
        if bar.volume == 0 or bar.high <= bar.low:
            return None

        delta = bar.delta
        delta_ratio = abs(delta) / bar.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = bar.high - bar.low

        # Check for significant wick opposite to flow
        has_wick = False
        
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = bar.close - bar.low
            wick_ratio = lower_wick / range_size
            if wick_ratio >= self.min_wick_ratio:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = bar.high - bar.close
            wick_ratio = upper_wick / range_size
            if wick_ratio >= self.min_wick_ratio:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(bar.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Return absorption info
        return {
            "direction": "long" if delta > 0 else "short",
            "absorption_close": bar.close,
            "absorption_high": bar.high,
            "absorption_low": bar.low,
        }

    def _check_failed_extension(self, current, absorption) -> Optional[dict]:
        """
        Mean-reversion signal: absorption was NOT followed by extension.
        
        If absorption was buying pressure (long), we enter SHORT if price
        fails to extend up or reverses down. This is contrarian to the
        absorption direction.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        if absorption["direction"] == "long":
            # Absorption was buying pressure at high
            # Failed extension: price doesn't reach new high, or closes lower
            
            # Check if price extended up (failed extension means it didn't)
            extended_up = current.high > absorption["absorption_high"]
            
            # Check if price reversed down
            reversed_down = current.close < absorption["absorption_close"]
            
            # We enter SHORT if either: no extension OR reversal
            if not (extended_up or reversed_down):
                # Price stayed around absorption level - not a clear failure
                return None
            
            # Enter SHORT (against the absorption direction)
            stop_price = absorption["absorption_high"] + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # absorption direction was short
            # Absorption was selling pressure at low
            # Failed extension: price doesn't reach new low, or closes higher
            
            extended_down = current.low < absorption["absorption_low"]
            reversed_up = current.close > absorption["absorption_close"]
            
            if not (extended_down or reversed_up):
                return None
            
            # Enter LONG (against the absorption direction)
            stop_price = absorption["absorption_low"] - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on mean-reversion signal."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption-meanrevert",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption-meanrevert",
            )
