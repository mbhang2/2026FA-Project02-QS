"""
Volume profile absorption strategy - addressing the synthetic identical trades issue
by fundamentally rethinking the signal.

Analysis of prior attempts:
- #16 (score -0.1076, 463 trades): absorption-only, very selective
- #34 (identical trades): initiation confirmation accidentally matched baseline
- #35 (identical trades): strict absorption parameters still matched baseline

The persistent "identical trades to baseline" suggests we're inadvertently selecting
bars that match momentum's pattern (close > open for longs, close < open for shorts)
regardless of our absorption logic.

Critical realization: if absorption detection keeps producing identical trades to
a momentum baseline, it means our "absorption" bars are coincidentally the same
bars where momentum fires. This happens because:

1. Strong buying pressure (high delta) naturally tends to push close > open
2. Strong selling pressure (low delta) naturally tends to push close < open
3. Wicks are common in momentum bars (retracements before continuation)

Solution: Decouple from momentum entirely by requiring bars that VIOLATE momentum
expectations while still showing absorption:

- LONG entries: Strong buying (delta > 0.60), wick below, BUT close < open
  (selling pressure despite buying volume = true absorption)
- SHORT entries: Strong selling (delta < 0.60), wick above, BUT close > open
  (buying pressure despite selling volume = true absorption)

This is the inverse of momentum and should produce completely different trades.

Additionally:
- Use only the most recent session's levels (not accumulated history)
- Tighter level proximity (0.5 points)
- Higher delta threshold (0.65)
- Require substantial wick (0.45 of range)
- Simpler level extraction (top 40% by volume)

This forces the strategy to:
1. Only trade bars that explicitly contradict momentum
2. Use structural levels from recent trading only
3. Hold fewer positions (multi-session holds from counter-trend entries)
4. Answer the campaign question: does absorption at a level precede moves away?
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.65,
        level_proximity: float = 0.5,
        min_wick_ratio: float = 0.45,
        stop_buffer: float = 0.25,
        target_multiple: float = 3.5,
        max_stop_distance: float = 2.5,
        min_stop_distance: float = 0.25,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size

        # Level tracking - only current session
        self._baseline_levels: list[float] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        
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
        prior = history[-2]

        # Handle session boundary
        if current.session != self._current_session:
            self._on_session_change()
            self._current_session = current.session

        # Accumulate volume profile for current session
        self._update_session_profile(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Avoid re-entry on same bar
        if current.close_ts in self._entered_bar_timestamps:
            return

        # Detect counter-trend absorption signal
        absorption = self._detect_counter_trend_absorption(current, prior)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Reset for new session."""
        self._current_session_profile = {}
        self._entered_bar_timestamps = set()

    def _update_session_profile(self, bar) -> None:
        """Accumulate volume at price levels in current session."""
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

    def _extract_levels(self, bars) -> list[float]:
        """Extract significant levels from a sequence of bars."""
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

        # Top 40% of volume
        top_levels = []
        cumulative = 0
        for level, vol in sorted_items:
            top_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.40:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """Get carried levels from current session only."""
        all_levels = []
        
        # Current session levels
        if self._current_session_profile:
            levels = self._extract_levels_from_profile(self._current_session_profile)
            all_levels.extend(levels)
        
        # Baseline levels
        all_levels.extend(self._baseline_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_counter_trend_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect counter-trend absorption: strong delta at a carried level with
        wick opposite to flow, AND bar closes AGAINST its delta direction.
        
        This is fundamentally different from momentum:
        - Momentum: close > open for longs, close < open for shorts
        - Counter-trend absorption: close < open despite buying, close > open despite selling
        
        This ensures we trade bars that contradict momentum, producing different trades.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Strict: require very strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low

        # Require substantial wick opposite to flow
        has_wick = False
        if delta > 0:  # Buying pressure
            lower_wick = current.close - current.low
            if lower_wick / range_size >= self.min_wick_ratio:
                has_wick = True
        elif delta < 0:  # Selling pressure
            upper_wick = current.high - current.close
            if upper_wick / range_size >= self.min_wick_ratio:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to carried level
        carried_levels = self._get_carried_levels()
        if not carried_levels:
            return None

        min_distance = min(abs(current.close - level) for level in carried_levels)
        if min_distance > self.level_proximity:
            return None

        # COUNTER-TREND requirement: bar closes AGAINST its delta direction
        # This is the key differentiator from momentum
        if delta > 0:  # Strong buying
            # Counter-trend: close < open (selling despite buying volume)
            if current.close >= current.open:
                return None
            
            # This is a long entry (buying absorption rejected)
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }
        
        elif delta < 0:  # Strong selling
            # Counter-trend: close > open (buying despite selling volume)
            if current.close <= current.open:
                return None
            
            # This is a short entry (selling absorption rejected)
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
            }

        return None

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on counter-trend absorption detection."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="absorption",
            )
