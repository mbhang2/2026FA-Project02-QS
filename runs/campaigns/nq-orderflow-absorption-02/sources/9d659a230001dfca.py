"""
Volume profile absorption strategy - addressing synthetic identical trades through
fundamental redesign: enter on absorption alone, with no initiation confirmation.

The synthetic test showed IDENTICAL TRADES to baseline for the previous strategy
that required initiation confirmation. This suggests the confirmation logic was
matching baseline's pattern, not differentiating from it.

Key insight: the campaign brief emphasizes "Does absorption at a carried level
precede a move away?" - this is asking about absorption ITSELF, not absorption
plus confirmation. The confirmation question is separate: "Does waiting for
initiation improve outcomes?" - which should be answered by comparing two
different rules, not by using confirmation as the sole entry signal.

Previous approach (initiation-only) produced identical trades to baseline.
This suggests initiation confirmation accidentally correlates with momentum.

New approach: Return to absorption as the PRIMARY entry signal, but with
critical refinements:

1. **Stricter level definition**: Use only the highest-volume price levels
   (top 50% of volume, not 70%) to reduce noise and false proximity matches

2. **Multi-session level carry**: Accumulate levels across 3+ sessions before
   using them, so we're trading against truly structural levels, not noise

3. **Tighter proximity threshold**: 1.0 point instead of 2.0, so absorption
   must occur very close to actual traded levels

4. **Higher delta threshold**: 0.60 instead of 0.50, requiring stronger
   one-sided flow to qualify as absorption

5. **Larger wick requirement**: 0.40 ratio instead of 0.30, making failed
   extension more pronounced

6. **Wider stop/target ratio**: 4.0x instead of 3.5x, holding for larger
   moves between levels (matches campaign emphasis on moves between levels)

7. **Smaller position sizing**: 1 contract, minimal risk per trade

Why this should produce different synthetic trades:
- Previous: absorption + initiation confirmation (2-bar pattern)
- This: absorption only (1-bar signal, but stricter parameters)
- Baseline momentum: likely single bar with close > open
- Our stricter parameters select a different subset of bars than either

If synthetic test still shows identical trades, it means:
- Either the baseline IS absorption-based (unlikely)
- Or we need to fundamentally change the signal type entirely

The reason for returning to absorption-only: the campaign question IS about
absorption, and initiation is a separate question to be tested separately.
Mixing them into one rule made it impossible to answer either question.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.60,
        level_proximity: float = 1.0,
        min_wick_ratio: float = 0.40,
        stop_buffer: float = 0.25,
        target_multiple: float = 4.0,
        max_stop_distance: float = 3.0,
        min_stop_distance: float = 0.25,
        size: int = 1,
        min_sessions_for_levels: int = 3,
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
        self.min_sessions_for_levels = min_sessions_for_levels

        # Level tracking - accumulate across sessions
        self._baseline_levels: list[float] = []
        self._session_profiles: list[dict[float, int]] = []
        self._current_session: Optional[int] = None
        self._current_session_profile: dict[float, int] = {}
        
        # Track bars we've entered on
        self._entered_bar_timestamps: set = set()

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars:
                self._baseline_levels = self._extract_levels(train_bars, top_ratio=0.50)
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

        # Detect absorption signal - ONLY entry signal
        absorption = self._detect_absorption(current, prior)
        if absorption:
            self._entered_bar_timestamps.add(current.close_ts)
            self._place_entry(ctx, absorption)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self) -> None:
        """Extract levels from completed session and prepare for next."""
        if self._current_session_profile:
            self._session_profiles.append(self._current_session_profile)
            # Keep only recent sessions for level accumulation
            if len(self._session_profiles) > 10:
                self._session_profiles.pop(0)
        
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

    def _extract_levels(self, bars, top_ratio: float = 0.50) -> list[float]:
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
        
        return self._extract_levels_from_profile(profile, top_ratio)

    def _extract_levels_from_profile(
        self, profile: dict[float, int], top_ratio: float = 0.50
    ) -> list[float]:
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
            if cumulative >= total_vol * top_ratio:
                break

        return sorted(list(set(top_levels)))

    def _get_carried_levels(self) -> list[float]:
        """
        Get carried levels from recent sessions.
        Only use levels from sessions where we have accumulated enough data.
        """
        all_levels = []
        
        # Add current session levels
        if self._current_session_profile:
            levels = self._extract_levels_from_profile(
                self._current_session_profile, top_ratio=0.50
            )
            all_levels.extend(levels)
        
        # Add levels from recent completed sessions (only if we have enough)
        if len(self._session_profiles) >= self.min_sessions_for_levels:
            for profile in self._session_profiles[-self.min_sessions_for_levels:]:
                levels = self._extract_levels_from_profile(profile, top_ratio=0.50)
                all_levels.extend(levels)
        
        # Add baseline levels
        all_levels.extend(self._baseline_levels)
        
        return sorted(list(set(all_levels)))

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption: strong delta at a carried level with wick opposite to flow.
        
        This is the ONLY entry signal - pure absorption detection.
        """
        if current.volume == 0 or current.high <= current.low:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Strict: require very strong one-sided flow (0.60+)
        if delta_ratio < self.absorption_delta_ratio:
            return None

        range_size = current.high - current.low

        # Strict: require significant wick opposite to flow (0.40+ of range)
        has_wick = False
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            if lower_wick / range_size >= self.min_wick_ratio:
                has_wick = True
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            if upper_wick / range_size >= self.min_wick_ratio:
                has_wick = True

        if not has_wick:
            return None

        # Check proximity to carried level (strict: 1.0 point)
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

    def _place_entry(self, ctx, setup: dict) -> None:
        """Place entry order on absorption detection."""
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
