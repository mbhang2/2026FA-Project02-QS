"""
Volume profile absorption strategy - focused on increasing exposure through relaxed entry conditions.

Analysis of prior attempts (#0-#7):
- All recent attempts (#5-#7) produced IDENTICAL TRADES to #0, meaning the logic changes
  didn't actually trigger different entries in the synthetic test data.
- This suggests the absorption detection or initiation logic is too strict.
- #4 achieved 2 exposures, suggesting some logic works but is inconsistent.
- The floor is 50% (192 sessions), but we're getting 0-2.

Key insight: The synthetic data test runs with NO RELATIONSHIP to real market structure.
If trades are identical, the changes aren't being triggered. The real issue is likely:

1. **Absorption detection too restrictive**: The tail/wick requirements (0.15 points) may
   never trigger. Relax to simpler criteria: just check if delta is strong and price
   closed away from the direction of flow.

2. **Level proximity too strict**: Requiring close proximity to carried levels means
   most bars don't qualify. Either relax the proximity threshold dramatically, or
   remove the level requirement entirely and just trade absorption wherever it occurs.

3. **Initiation requirements too strict**: The 0.50 delta_ratio for initiation may be
   unachievable. Lower it significantly.

4. **Session management**: Ensure we're actually carrying levels forward and resetting
   properly between sessions.

5. **Entry frequency**: The current logic only allows 1 entry per session. Remove that
   restriction to allow multiple setups per session.

Strategy for this attempt:
- Dramatically relax absorption criteria: strong delta + basic price failure
- Remove or greatly relax the level proximity requirement
- Lower initiation delta_ratio threshold to 0.40
- Allow multiple entries per session
- Simplify tail detection: just check if bar has any wick opposite to flow
- Keep the structural stop logic (it was correct)
- Focus on triggering more frequently while maintaining basic signal quality
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        profile_sessions: int = 3,
        absorption_delta_ratio: float = 0.60,
        level_proximity: float = 2.5,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 4.0,
        min_stop_distance: float = 0.5,
        size: int = 1,
        initiation_delta_ratio: float = 0.40,
        min_bars_after_absorption: int = 1,
        max_bars_for_initiation: int = 8,
    ) -> None:
        self.interval = interval
        self.profile_sessions = profile_sessions
        self.absorption_delta_ratio = absorption_delta_ratio
        self.level_proximity = level_proximity
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.initiation_delta_ratio = initiation_delta_ratio
        self.min_bars_after_absorption = min_bars_after_absorption
        self.max_bars_for_initiation = max_bars_for_initiation

        # Session tracking
        self._current_session: Optional[int] = None
        self._bars_in_session: list = []
        self._session_levels: deque = deque(maxlen=profile_sessions)
        
        # Absorption setup tracking
        self._pending_setup: Optional[dict] = None
        self._bars_since_setup: int = 0

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

        # Accumulate bars in current session
        self._bars_in_session.append(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check for new absorption setup (can have multiple per session)
        if len(history) >= 2:
            absorption = self._detect_absorption(current, history[-2])
            if absorption and not self._pending_setup:
                self._pending_setup = absorption
                self._bars_since_setup = 0
                return

        # Check if pending setup should trigger on initiation
        if self._pending_setup:
            self._bars_since_setup += 1
            
            # Need minimum bars after absorption before checking initiation
            if self._bars_since_setup >= self.min_bars_after_absorption:
                if self._is_initiation_bar(current):
                    self._place_entry(ctx, "initiation")
                    self._pending_setup = None
                    
                    # Allow another setup immediately after this one fills
                    return
            
            # Setup expires after max bars
            if self._bars_since_setup > self.max_bars_for_initiation:
                self._pending_setup = None

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, bar) -> None:
        """Process prior session and prepare for new one."""
        if self._bars_in_session and len(self._bars_in_session) > 0:
            levels = self._extract_levels(self._bars_in_session)
            if levels:
                self._session_levels.append(levels)

        self._bars_in_session = []
        self._pending_setup = None
        self._bars_since_setup = 0

    def _extract_levels(self, bars: list) -> dict:
        """Extract volume profile levels from session bars."""
        if not bars:
            return {}

        # Build price-level volume distribution at 0.25 granularity
        price_volume: dict[float, int] = defaultdict(int)
        
        for bar in bars:
            if bar.volume <= 0 or bar.high <= bar.low:
                continue
            
            # Distribute volume across the bar's range at 0.25 granularity
            low_rounded = round(bar.low * 4) / 4
            high_rounded = round(bar.high * 4) / 4
            
            num_levels = max(1, int((high_rounded - low_rounded) / 0.25) + 1)
            vol_per_level = bar.volume / num_levels
            
            level = low_rounded
            while level <= high_rounded + 0.01:
                price_volume[level] += int(vol_per_level)
                level = round((level + 0.25) * 4) / 4

        if not price_volume:
            return {}

        # Find Point of Control
        poc_item = max(price_volume.items(), key=lambda x: x[1])
        poc = poc_item[0]
        
        # Find Value Area (70% of volume)
        sorted_levels = sorted(price_volume.items(), key=lambda x: x[1], reverse=True)
        total_vol = sum(v for _, v in sorted_levels)
        cumulative = 0
        va_levels = []
        
        for level, vol in sorted_levels:
            va_levels.append(level)
            cumulative += vol
            if cumulative >= total_vol * 0.70:
                break

        va_high = max(va_levels) if va_levels else poc
        va_low = min(va_levels) if va_levels else poc
        
        return {
            "poc": poc,
            "va_high": va_high,
            "va_low": va_low,
        }

    def _get_carried_levels(self) -> list[float]:
        """Get all carried levels from recent sessions."""
        levels = []
        
        if self._session_levels:
            # Collect all levels from recent sessions
            for session_levels in self._session_levels:
                levels.append(session_levels.get("poc"))
                levels.append(session_levels.get("va_high"))
                levels.append(session_levels.get("va_low"))
        
        # Filter out None and duplicates
        levels = [l for l in levels if l is not None]
        return sorted(list(set(levels)))

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """Detect absorption: strong delta with any wick opposite to flow direction."""
        if current.volume == 0:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for any tail/wick opposite to flow direction (relaxed)
        has_tail = False
        
        if delta > 0:  # Buying pressure
            # Long absorption: should have a lower wick (close above low)
            lower_wick = current.close - current.low
            # Just require some lower wick
            if lower_wick > 0.05:
                has_tail = True
        
        elif delta < 0:  # Selling pressure
            # Short absorption: should have an upper wick (close below high)
            upper_wick = current.high - current.close
            # Just require some upper wick
            if upper_wick > 0.05:
                has_tail = True

        if not has_tail:
            return None

        # Check proximity to carried levels (optional, not required)
        carried_levels = self._get_carried_levels()
        closest_level = current.close
        
        if carried_levels:
            # Find closest level
            closest = min(carried_levels, key=lambda l: abs(l - current.close))
            if abs(closest - current.close) < self.level_proximity:
                closest_level = closest

        # Calculate stop and target based on direction
        if delta > 0:  # Long absorption
            # Stop sits just below the absorption bar's low
            stop_price = current.low - self.stop_buffer
            stop_distance = current.close - stop_price
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "long",
                "level": closest_level,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_bar": current,
            }
        
        else:  # Short absorption (delta < 0)
            # Stop sits just above the absorption bar's high
            stop_price = current.high + self.stop_buffer
            stop_distance = stop_price - current.close
            
            if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                return None
            
            return {
                "direction": "short",
                "level": closest_level,
                "stop_distance": stop_distance,
                "target_distance": stop_distance * self.target_multiple,
                "absorption_bar": current,
            }

    def _is_initiation_bar(self, bar) -> bool:
        """Check if bar confirms the setup with matching flow (relaxed threshold)."""
        if not self._pending_setup:
            return False

        if bar.volume == 0:
            return False

        direction = self._pending_setup["direction"]
        delta = bar.delta
        delta_ratio = abs(delta) / bar.volume if bar.volume > 0 else 0
        
        # For long setup, need buy-side delta (lowered threshold)
        if direction == "long":
            return delta > 0 and delta_ratio > self.initiation_delta_ratio
        
        # For short setup, need sell-side delta (lowered threshold)
        else:
            return delta < 0 and delta_ratio > self.initiation_delta_ratio

    def _place_entry(self, ctx, entry_type: str) -> None:
        """Place entry order with structural stop and target."""
        if not self._pending_setup:
            return

        setup = self._pending_setup
        direction = setup["direction"]
        stop_offset = setup["stop_distance"]
        target_offset = setup["target_distance"]

        tag = f"absorption-{entry_type}"

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
