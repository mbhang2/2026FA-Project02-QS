"""
Volume profile absorption strategy - refined for cost survival and realistic signal detection.

Analysis of prior attempts shows a critical pattern:
- #13 scored -0.9744 with fallback momentum (identical trades to baseline)
- #14-#15 produced 0-49 exposures (below 50% floor)
- Synthetic test shows identical trades, suggesting absorption detection fires but produces
  same entry/exit as baseline momentum

The core issue: previous attempts either:
1. Never entered (absorption detection too strict)
2. Entered on absorption but with targets too small (2.5x stop = 1-2 points)
3. Fell back to momentum, which is identical to baseline

This attempt focuses on:
1. **Pure absorption signal** - no fallback to momentum, no level requirement
2. **Aggressive entry confirmation** - wait 1-2 bars for confirming flow in same direction
3. **Cost-aware targets** - minimum 5 points per round turn to survive 0.2 points commission
4. **Simpler detection** - strong delta (0.45+) + wick opposite to flow direction
5. **Multi-session level accumulation** - carry levels forward but don't require proximity

Key change: Remove level proximity requirement entirely. Absorption at ANY price with
strong delta + wick is tradeable. This ensures the signal fires more often on real data.
The "absorption at carried levels" constraint was preventing entries.

Entry tagging: "absorption-alone" if we enter on absorption bar, "absorption-confirm" if
we wait for confirmation bar. This lets us measure if waiting helped.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.45,
        min_wick_ratio: float = 0.04,
        stop_buffer: float = 0.25,
        min_stop_distance: float = 0.5,
        max_stop_distance: float = 4.0,
        min_target_distance: float = 5.0,
        min_body_points: float = 0.3,
        wait_for_confirmation: bool = True,
        wait_bars_min: int = 1,
        wait_bars_max: int = 2,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.min_wick_ratio = min_wick_ratio
        self.stop_buffer = stop_buffer
        self.min_stop_distance = min_stop_distance
        self.max_stop_distance = max_stop_distance
        self.min_target_distance = min_target_distance
        self.min_body_points = min_body_points
        self.wait_for_confirmation = wait_for_confirmation
        self.wait_bars_min = wait_bars_min
        self.wait_bars_max = wait_bars_max
        self.size = size

        # Session state
        self._current_session: Optional[int] = None
        self._session_bars: list = []
        self._prior_session_levels: list[float] = []
        
        # Pending absorption setup waiting for confirmation
        self._pending_absorption: Optional[dict] = None
        self._bars_since_absorption: int = 0
        self._last_absorption_close_ts: int = -1

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
            self._on_session_change(current)
            self._current_session = current.session

        # Accumulate session bars
        self._session_bars.append(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check for absorption on this bar (only if no pending setup)
        if not self._pending_absorption and len(history) >= 2:
            absorption = self._detect_absorption(current, history[-2])
            if absorption and current.close_ts != self._last_absorption_close_ts:
                self._pending_absorption = absorption
                self._bars_since_absorption = 0
                self._last_absorption_close_ts = current.close_ts
                
                # Option 1: Enter immediately on absorption
                if not self.wait_for_confirmation:
                    self._place_entry(ctx, absorption, "absorption-alone")
                    self._pending_absorption = None
                return

        # Check for confirmation on pending setup
        if self._pending_absorption:
            self._bars_since_absorption += 1
            
            # Wait for confirmation bar
            if self._bars_since_absorption >= self.wait_bars_min:
                if self._is_confirming_bar(current):
                    self._place_entry(ctx, self._pending_absorption, "absorption-confirm")
                    self._pending_absorption = None
                    return
            
            # Expire setup if waited too long
            if self._bars_since_absorption > self.wait_bars_max:
                self._pending_absorption = None

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, bar) -> None:
        """Process end of session and prepare for new one."""
        # Extract and save levels from completed session
        if self._session_bars:
            levels = self._extract_levels(self._session_bars)
            if levels:
                self._prior_session_levels = levels
        
        self._session_bars = []
        self._pending_absorption = None
        self._bars_since_absorption = 0

    def _extract_levels(self, bars: list) -> list[float]:
        """Extract significant price levels from bars using volume profile."""
        if not bars:
            return []

        # Build volume profile at 0.25 granularity
        price_volume: dict[float, int] = defaultdict(int)
        
        for bar in bars:
            if bar.volume <= 0 or bar.high <= bar.low:
                continue
            
            low_rounded = round(bar.low * 4) / 4
            high_rounded = round(bar.high * 4) / 4
            
            num_buckets = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
            vol_per_bucket = bar.volume / num_buckets
            
            level = low_rounded
            while level <= high_rounded + 0.01:
                level_rounded = round(level * 4) / 4
                price_volume[level_rounded] += int(vol_per_bucket)
                level += 0.25
        
        if not price_volume:
            return []

        # Get top levels by volume (top 70%)
        sorted_items = sorted(price_volume.items(), key=lambda x: x[1], reverse=True)
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

        return sorted(top_levels)

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption: strong one-way delta with wick opposite to flow.
        Does NOT require proximity to a level - trades absorption anywhere.
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for wick opposite to flow direction (as ratio of bar range)
        bar_range = current.high - current.low
        if bar_range <= 0:
            return None

        has_tail = False
        
        if delta > 0:  # Buying pressure - need lower wick
            lower_wick = current.close - current.low
            wick_ratio = lower_wick / bar_range
            if wick_ratio >= self.min_wick_ratio:
                has_tail = True
        
        elif delta < 0:  # Selling pressure - need upper wick
            upper_wick = current.high - current.close
            wick_ratio = upper_wick / bar_range
            if wick_ratio >= self.min_wick_ratio:
                has_tail = True

        if not has_tail:
            return None

        # Calculate stop distance
        if delta > 0:  # Long
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
        else:  # Short
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close

        if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
            return None

        # Target: at least min_target_distance, or next level away
        target_distance = self.min_target_distance
        
        # Try to find next level to target
        all_levels = self._prior_session_levels + self._extract_levels(self._session_bars)
        
        if delta > 0 and all_levels:  # Long: find next level above
            above_levels = [l for l in all_levels if l > current.close]
            if above_levels:
                target_level = min(above_levels)
                level_distance = target_level - current.close
                if level_distance > target_distance:
                    target_distance = level_distance
        
        elif delta < 0 and all_levels:  # Short: find next level below
            below_levels = [l for l in all_levels if l < current.close]
            if below_levels:
                target_level = max(below_levels)
                level_distance = current.close - target_level
                if level_distance > target_distance:
                    target_distance = level_distance

        return {
            "direction": "long" if delta > 0 else "short",
            "stop_distance": stop_distance,
            "target_distance": target_distance,
        }

    def _is_confirming_bar(self, bar) -> bool:
        """
        Check if bar confirms the pending absorption setup.
        Must show: same direction flow and meaningful body.
        """
        if not self._pending_absorption or bar.volume == 0:
            return False

        direction = self._pending_absorption["direction"]
        delta = bar.delta

        # Must have flow in the same direction
        if direction == "long" and delta <= 0:
            return False
        if direction == "short" and delta >= 0:
            return False

        # Check body size (absolute points)
        body = abs(bar.close - bar.open)
        if body < self.min_body_points:
            return False

        return True

    def _place_entry(self, ctx, setup: dict, tag: str) -> None:
        """Place entry order with structural stop and target."""
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
