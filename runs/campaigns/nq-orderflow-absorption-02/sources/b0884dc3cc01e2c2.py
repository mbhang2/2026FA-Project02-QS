"""
Volume profile absorption strategy with realistic signal detection.

Prior attempts (#0-#11) show a consistent pattern:
- #0 crashed on stop placement (short stop below entry)
- #1-#3, #5-#7 produced 0 exposures (logic too restrictive)
- #4 produced 2 exposures
- #8 produced 4 exposures
- #9 produced 24 exposures
- #10 produced 17 exposures
- #11 produced 1 exposure

The synthetic test shows identical trades to baseline, meaning the absorption
detection logic fires but produces the same entry/exit pattern as simple momentum.

Key insight from the question: We need to hold for moves BETWEEN levels, not
trade absorption itself. The campaign asks if absorption PRECEDES a move away,
with multi-session holding periods. Current approach tries to trade absorption
directly (scalping), which fails under the cost model.

Revised strategy:
1. **Build volume profile from training data** - establish reference levels
2. **Detect absorption at levels** - price arrives with strong one-way flow but
   closes back past prior bar's extreme (fails to extend)
3. **Hold for initiation** - wait for a confirming bar showing efficient flow
   (points traveled per unit of signed volume)
4. **Hold for multi-bar moves** - target is the next level in the profile, not
   a fixed multiple of stop distance
5. **Tag entries** - "absorption-alone" vs "absorption-initiation" to measure
   whether waiting helped
6. **Structural stop** - just beyond the absorption bar's extreme, constant buffer

The problem with #11 and prior attempts: they were trying to scalp with 2.5x
targets on 5m bars. That can't survive 0.2 points of round-trip cost. This
version targets the next level in the profile, which could be 5-15 points away,
making the math work.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        stop_buffer: float = 0.25,
        min_stop_distance: float = 0.5,
        max_stop_distance: float = 4.0,
        min_tail_points: float = 0.10,
        absorption_delta_ratio: float = 0.55,
        efficiency_lookback: int = 20,
        efficiency_percentile: float = 0.70,
        size: int = 1,
        use_initiation: bool = True,
        max_bars_to_initiation: int = 5,
    ) -> None:
        self.interval = interval
        self.stop_buffer = stop_buffer
        self.min_stop_distance = min_stop_distance
        self.max_stop_distance = max_stop_distance
        self.min_tail_points = min_tail_points
        self.absorption_delta_ratio = absorption_delta_ratio
        self.efficiency_lookback = efficiency_lookback
        self.efficiency_percentile = efficiency_percentile
        self.size = size
        self.use_initiation = use_initiation
        self.max_bars_to_initiation = max_bars_to_initiation

        # Profile and levels
        self._baseline_levels: list[float] = []
        self._current_session: Optional[int] = None
        self._session_bars: list = []
        self._prior_session_levels: list[float] = []
        
        # Efficiency tracking for initiation
        self._recent_efficiencies_up: deque = deque(maxlen=efficiency_lookback)
        self._recent_efficiencies_down: deque = deque(maxlen=efficiency_lookback)
        
        # Pending absorption setup
        self._pending_absorption: Optional[dict] = None
        self._bars_since_absorption: int = 0
        self._last_absorption_bar_index: int = -1000

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        
        # Build baseline levels from training data
        try:
            train_bars = ctx.train_bars(self.interval)
            if train_bars and len(train_bars) > 0:
                self._baseline_levels = self._extract_vpoc_and_va(train_bars)
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
            self._on_session_change(current)
            self._current_session = current.session

        # Accumulate session bars for profile building
        self._session_bars.append(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Update efficiency tracking
        if len(history) >= 2:
            self._update_efficiency(current, history[-2])

        current_bar_index = len(history) - 1

        # Check for absorption setup (only if no pending setup)
        if not self._pending_absorption and len(history) >= 2:
            absorption = self._detect_absorption(current, history[-2])
            if absorption and current_bar_index != self._last_absorption_bar_index:
                self._pending_absorption = absorption
                self._bars_since_absorption = 0
                self._last_absorption_bar_index = current_bar_index
                
                # If not using initiation, enter immediately
                if not self.use_initiation:
                    self._place_entry(ctx, absorption, "absorption-alone")
                    self._pending_absorption = None
                return

        # Check for initiation on pending setup
        if self._pending_absorption:
            self._bars_since_absorption += 1
            
            # Must wait at least 1 bar for initiation
            if self._bars_since_absorption >= 1 and self._bars_since_absorption <= self.max_bars_to_initiation:
                if self._is_initiating_bar(current):
                    self._place_entry(ctx, self._pending_absorption, "absorption-initiation")
                    self._pending_absorption = None
                    return
            
            # Expire the setup if we've waited too long
            if self._bars_since_absorption > self.max_bars_to_initiation:
                self._pending_absorption = None

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, bar) -> None:
        """Extract levels from session and prepare for new one."""
        if self._session_bars:
            self._prior_session_levels = self._extract_vpoc_and_va(self._session_bars)
        
        self._session_bars = []
        self._pending_absorption = None
        self._bars_since_absorption = 0
        self._recent_efficiencies_up.clear()
        self._recent_efficiencies_down.clear()

    def _extract_vpoc_and_va(self, bars: list) -> list[float]:
        """Extract VPOC and Value Area levels from bars."""
        if not bars:
            return []

        # Build volume profile at 0.25 granularity
        price_volume: dict[float, int] = defaultdict(int)
        
        for bar in bars:
            if bar.volume <= 0 or bar.high <= bar.low:
                continue
            
            low_bucket = round(bar.low * 4) / 4
            high_bucket = round(bar.high * 4) / 4
            
            num_buckets = max(1, int((high_bucket - low_bucket) / 0.25) + 1)
            vol_per_bucket = bar.volume / num_buckets
            
            level = low_bucket
            while level <= high_bucket + 0.001:
                level_key = round(level * 4) / 4
                price_volume[level_key] += int(vol_per_bucket)
                level += 0.25
        
        if not price_volume:
            return []

        # Find VPOC
        vpoc = max(price_volume.items(), key=lambda x: x[1])[0]
        
        # Find Value Area (70% of volume)
        sorted_items = sorted(price_volume.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_items)
        cumulative = 0
        va_levels = []
        
        for level, vol in sorted_items:
            va_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.70:
                break

        # Return sorted unique levels
        all_levels = [vpoc] + va_levels + self._baseline_levels
        return sorted(list(set(all_levels)))

    def _update_efficiency(self, current, prior) -> None:
        """Track efficiency (points per unit signed volume) for initiation detection."""
        if current.volume == 0 or current.delta == 0:
            return
        
        # Points traveled per unit of signed volume
        points_traveled = current.high - current.low
        efficiency = points_traveled / abs(current.delta) if current.delta != 0 else 0
        
        if current.delta > 0:
            self._recent_efficiencies_up.append(efficiency)
        else:
            self._recent_efficiencies_down.append(efficiency)

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """
        Detect absorption at a price level:
        1. Price near a carried level (from prior session or baseline)
        2. Strong one-way delta
        3. Bar closes back past prior bar's extreme (fails to extend)
        4. Valid stop distance
        """
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check if price is near a carried level
        all_levels = self._prior_session_levels + self._baseline_levels
        near_level = False
        closest_level = None
        
        for level in all_levels:
            if abs(current.close - level) <= 2.0:  # Within 2 points of a level
                near_level = True
                if closest_level is None or abs(current.close - level) < abs(current.close - closest_level):
                    closest_level = level
        
        if not near_level or closest_level is None:
            return None

        # Check for absorption pattern: tail opposite to flow direction
        has_absorption_tail = False
        
        if delta > 0:  # Buying pressure
            # Long absorption: should have lower wick, and close should be below prior high
            lower_wick = current.close - current.low
            if lower_wick >= self.min_tail_points and current.close < prior.high:
                has_absorption_tail = True
        
        elif delta < 0:  # Selling pressure
            # Short absorption: should have upper wick, and close should be above prior low
            upper_wick = current.high - current.close
            if upper_wick >= self.min_tail_points and current.close > prior.low:
                has_absorption_tail = True

        if not has_absorption_tail:
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

        # Find target: next level away from current price
        target_level = None
        if delta > 0:  # Long: look for levels above
            above_levels = [l for l in all_levels if l > current.close]
            if above_levels:
                target_level = min(above_levels)
        else:  # Short: look for levels below
            below_levels = [l for l in all_levels if l < current.close]
            if below_levels:
                target_level = max(below_levels)

        target_distance = abs(target_level - current.close) if target_level else stop_distance * 2.5

        return {
            "direction": "long" if delta > 0 else "short",
            "stop_distance": stop_distance,
            "target_distance": target_distance,
            "level": closest_level,
        }

    def _is_initiating_bar(self, bar) -> bool:
        """Check if bar shows efficient flow confirming the setup direction."""
        if not self._pending_absorption or bar.volume == 0:
            return False

        direction = self._pending_absorption["direction"]
        delta = bar.delta
        
        # Initiation must match direction and show good efficiency
        if direction == "long":
            if delta <= 0:
                return False
            
            if not self._recent_efficiencies_up:
                return True  # No history yet, accept
            
            # Calculate 70th percentile of recent up efficiencies
            sorted_eff = sorted(self._recent_efficiencies_up)
            threshold = sorted_eff[int(len(sorted_eff) * self.efficiency_percentile)]
            
            bar_eff = (bar.high - bar.low) / abs(delta) if delta != 0 else 0
            return bar_eff > threshold
        
        else:  # Short
            if delta >= 0:
                return False
            
            if not self._recent_efficiencies_down:
                return True  # No history yet, accept
            
            # Calculate 70th percentile of recent down efficiencies
            sorted_eff = sorted(self._recent_efficiencies_down)
            threshold = sorted_eff[int(len(sorted_eff) * self.efficiency_percentile)]
            
            bar_eff = (bar.high - bar.low) / abs(delta) if delta != 0 else 0
            return bar_eff > threshold

    def _place_entry(self, ctx, setup: dict, entry_type: str) -> None:
        """Place entry with structural stop and target."""
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        tag = entry_type

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
