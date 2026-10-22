"""
Volume profile absorption strategy - diagnostic refinement to understand synthetic behavior.

Analysis of the campaign:
- #16 scored -0.1076 with 463 trades (best real score achieved)
- ALL recent trials produce IDENTICAL 70 synthetic trades
- This suggests the synthetic baseline has a fixed fallback that we keep matching
- The identical synthetic behavior across parameter changes is the key diagnostic

Critical insight: If synthetic data produces identical 70 trades across all variations,
it means either:
1. The absorption detection never fires on synthetic data (synthetic has no absorption)
2. There's a fallback mechanism we're not seeing that fires instead
3. The synthetic baseline itself IS absorption-based (unlikely given 70 is fixed)

Since #16 produced 463 trades on real data but we get 70 on synthetic, the
absorption signal exists on real data but not synthetic.

New approach - add explicit momentum fallback ONLY when absorption doesn't fire:
This will help us understand what's generating the 70 synthetic trades and whether
we can improve on #16's real performance by being more selective about when we
trade the fallback.

Key changes from current:
1. Keep absorption detection identical to #16 (delta_ratio=0.35, proximity=4.0)
2. Add momentum fallback: only trade close > open / close < open when absorption doesn't fire
3. Make fallback conservative: require both momentum AND proximity to a level
4. Tag entries differently: "absorption" vs "momentum-fallback" for diagnostic visibility
5. Reduce fallback frequency: only act on momentum once per bar, same bar-tracking as absorption

The reason: If the 70 synthetic trades are from fallback, this helps us see it clearly
in the tags. If they're from absorption, we'll see "absorption" tags and understand
why synthetic differs from real. Either way, we get diagnostic information.

On real data, this should maintain #16's 463+ trades (absorption) plus selective
momentum fallback when absorption doesn't fire, which could improve the score if
momentum adds signal or hurt it if it adds noise.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.35,
        level_proximity: float = 4.0,
        min_wick_points: float = 0.0,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 5.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        level_percentile: float = 0.65,
        momentum_fallback_enabled: bool = True,
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
        self.level_percentile = level_percentile
        self.momentum_fallback_enabled = momentum_fallback_enabled

        # Level tracking
        self._baseline_levels: list[float] = []
        self._session_levels: dict[float, int] = {}
        self._current_session: Optional[int] = None
        self._all_prior_levels: set[float] = set()
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

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
        if len(history) < 1:
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

        # Try absorption first - this is the primary signal
        absorption = self._detect_absorption(current)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption, tag="absorption")
            return

        # Fallback to momentum only if absorption didn't fire and enabled
        if self.momentum_fallback_enabled and len(history) >= 2:
            momentum = self._detect_momentum_fallback(current)
            if momentum:
                self._entered_bar_timestamps.add(current.close_ts)
                self._place_entry(ctx, momentum, tag="momentum-fallback")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract levels from session and prepare for next."""
        if self._session_levels:
            levels = self._extract_levels_from_profile(self._session_levels)
            self._all_prior_levels.update(levels)
        
        self._session_levels = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels."""
        if bar.volume <= 0 or bar.high <= bar.low:
            return

        # Distribute volume across 0.25-point granularity
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

        # Get top levels comprising specified percentile of volume
        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * self.level_percentile:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from current and prior sessions."""
        all_levels = set()
        all_levels.update(self._extract_levels_from_profile(self._session_levels))
        all_levels.update(self._all_prior_levels)
        all_levels.update(self._baseline_levels)
        return sorted(list(all_levels))

    def _detect_absorption(self, current) -> Optional[dict]:
        """
        Detect absorption: strong delta at a carried level with wick opposite to flow.
        
        Parameters match #16 which scored -0.1076 with 463 trades.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow (0.35 - what worked in #16)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for wick opposite to flow direction
        has_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick >= self.min_wick_points:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick >= self.min_wick_points:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to carried level (4.0 - what worked in #16)
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
            
            # Validate stop distance
            if stop_distance <= 0 or not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        else:  # Short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            # Validate stop distance
            if stop_distance <= 0 or not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

    def _detect_momentum_fallback(self, current) -> Optional[dict]:
        """
        Fallback momentum signal: trade directional close when absorption doesn't fire.
        Conservative version that requires proximity to a level.
        """
        if current.volume == 0:
            return None

        # Check proximity to carried level - require it for fallback too
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # Momentum: simple close > open or close < open
        if current.close > current.open:
            # Long momentum
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if stop_distance <= 0 or not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        elif current.close < current.open:
            # Short momentum
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if stop_distance <= 0 or not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        return None

    def _place_entry(self, ctx, setup: dict, tag: str = "entry") -> None:
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
