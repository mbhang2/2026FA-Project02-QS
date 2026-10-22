"""
Volume profile absorption strategy - refined with focus on signal strength.

This strategy investigates whether absorption (strong one-way flow that fails to 
move price) at volume profile levels precedes directional moves.

Key refinements from prior attempts:
1. Stricter absorption detection: require both strong delta AND clear price failure
2. Better level identification: track actual volume-weighted price levels, not just POC
3. Improved efficiency tracking: separate tracking for up/down moves with better thresholds
4. More selective entry: only trade when conditions are genuinely strong
5. Better session management: properly carry levels across sessions
6. Structural stops: fixed buffer beyond absorption bar extreme
7. Tag entries: absorption-only vs post-confirmation for analysis

The prior attempts (#1, #2, #3) exposed in too few sessions. This version focuses
on signal quality over frequency - better to act rarely on strong setups than often
on weak ones. The key changes:
- Raise absorption delta threshold to 0.85 (was 0.75)
- Require tighter price proximity to profile levels (0.25 points)
- Stricter initiation: require both high efficiency AND large body
- Only use POC from prior session (not all sessions)
- Better handling of edge cases in profile building
"""

from __future__ import annotations

from collections import deque, defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        absorption_delta_ratio: float = 0.85,
        efficiency_percentile: float = 0.80,
        efficiency_window: int = 25,
        stop_buffer: float = 0.25,
        target_multiple: float = 2.5,
        max_stop_distance: float = 2.5,
        min_stop_distance: float = 0.75,
        size: int = 1,
        level_proximity: float = 0.25,
    ) -> None:
        self.interval = interval
        self.absorption_delta_ratio = absorption_delta_ratio
        self.efficiency_percentile = efficiency_percentile
        self.efficiency_window = efficiency_window
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.max_stop_distance = max_stop_distance
        self.min_stop_distance = min_stop_distance
        self.size = size
        self.level_proximity = level_proximity

        # Session tracking
        self._current_session: Optional[int] = None
        self._bars_in_session: list = []
        
        # Volume profile levels from prior session
        self._prior_poc: Optional[float] = None
        self._prior_va_high: Optional[float] = None
        self._prior_va_low: Optional[float] = None
        
        # Bar efficiency tracking (separate for up/down delta)
        self._efficiencies_up: deque = deque(maxlen=efficiency_window)
        self._efficiencies_down: deque = deque(maxlen=efficiency_window)
        
        # Absorption setup tracking
        self._pending_setup: Optional[dict] = None
        self._bars_since_setup: int = 0
        self._last_absorption_bar_index: int = -1000

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

        # Update efficiency tracking from this bar
        self._update_efficiency(current)

        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return

        # Check for absorption setup on this bar (only if we haven't just detected one)
        current_bar_index = len(history) - 1
        if len(history) >= 2 and current_bar_index != self._last_absorption_bar_index:
            absorption = self._detect_absorption(current, history[-2])
            if absorption:
                self._pending_setup = absorption
                self._bars_since_setup = 0
                self._last_absorption_bar_index = current_bar_index
                return

        # Check if pending setup should trigger or be abandoned
        if self._pending_setup:
            self._bars_since_setup += 1
            
            # Check for initiation confirmation
            if self._bars_since_setup >= 1 and self._is_initiation_bar(current):
                self._place_entry(ctx, "initiation")
                self._pending_setup = None
            elif self._bars_since_setup > 4:
                # Setup expired - give it 4 bars to confirm
                self._pending_setup = None

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _on_session_change(self, bar) -> None:
        """Process prior session and prepare for new one."""
        if self._bars_in_session and len(self._bars_in_session) > 0:
            # Build volume profile from just-completed session
            self._build_profile(self._bars_in_session)

        self._bars_in_session = []
        self._pending_setup = None
        self._bars_since_setup = 0
        self._last_absorption_bar_index = -1000

    def _build_profile(self, bars: list) -> None:
        """Extract key volume levels from session bars."""
        if not bars or len(bars) == 0:
            return

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
            return

        # Find Point of Control (highest volume level)
        poc_item = max(price_volume.items(), key=lambda x: x[1])
        self._prior_poc = poc_item[0]
        
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

        if va_levels:
            self._prior_va_high = max(va_levels)
            self._prior_va_low = min(va_levels)

    def _detect_absorption(self, current, prior) -> Optional[dict]:
        """Detect absorption: strong delta at a level with clear price failure."""
        if current.volume == 0:
            return None

        # Check if price is at a carried level (POC from prior session)
        at_level = False
        level_price = None
        
        if self._prior_poc is not None and abs(current.close - self._prior_poc) < self.level_proximity:
            at_level = True
            level_price = self._prior_poc
        elif self._prior_va_high is not None and abs(current.close - self._prior_va_high) < self.level_proximity:
            at_level = True
            level_price = self._prior_va_high
        elif self._prior_va_low is not None and abs(current.close - self._prior_va_low) < self.level_proximity:
            at_level = True
            level_price = self._prior_va_low

        if not at_level or level_price is None:
            return None

        delta = current.delta
        delta_ratio = abs(delta) / current.volume

        # Need strong one-sided flow
        if delta_ratio < self.absorption_delta_ratio:
            return None

        # Check for clear price failure (tail formation)
        if delta > 0:  # Buying pressure
            # Long absorption: closes significantly below prior bar's low
            if current.close < prior.low - 0.1:
                # Stop distance: distance from entry down to absorption bar's low
                stop_distance = current.close - (current.low - self.stop_buffer)
                
                # Validate stop distance
                if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                    return None
                
                return {
                    "direction": "long",
                    "level": level_price,
                    "stop_distance": stop_distance,
                    "target_distance": stop_distance * self.target_multiple,
                    "absorption_bar": current,
                    "absorption_delta": delta,
                }
        else:  # Selling pressure
            # Short absorption: closes significantly above prior bar's high
            if current.close > prior.high + 0.1:
                # Stop distance: distance from entry up to absorption bar's high
                stop_distance = (current.high + self.stop_buffer) - current.close
                
                # Validate stop distance
                if not (self.min_stop_distance <= stop_distance <= self.max_stop_distance):
                    return None
                
                return {
                    "direction": "short",
                    "level": level_price,
                    "stop_distance": stop_distance,
                    "target_distance": stop_distance * self.target_multiple,
                    "absorption_bar": current,
                    "absorption_delta": abs(delta),
                }

        return None

    def _update_efficiency(self, bar) -> None:
        """Track bar efficiency: points traveled per unit signed flow."""
        if bar.volume == 0 or bar.delta == 0:
            return

        points = bar.high - bar.low
        efficiency = points / abs(bar.delta)

        if bar.delta > 0:
            self._efficiencies_up.append(efficiency)
        else:
            self._efficiencies_down.append(efficiency)

    def _is_initiation_bar(self, bar) -> bool:
        """Check if bar shows strong efficiency and body confirming the setup."""
        if not self._pending_setup:
            return False

        if bar.volume == 0 or bar.delta == 0:
            return False

        direction = self._pending_setup["direction"]
        
        # Select appropriate efficiency history based on direction
        if direction == "long":
            if len(self._efficiencies_up) < 5:
                return False
            efficiencies = list(self._efficiencies_up)
            # For long, we want upward delta confirmation
            bar_delta_ok = bar.delta > 0
        else:
            if len(self._efficiencies_down) < 5:
                return False
            efficiencies = list(self._efficiencies_down)
            # For short, we want downward delta confirmation
            bar_delta_ok = bar.delta < 0

        if not bar_delta_ok:
            return False

        # Current bar efficiency
        points = bar.high - bar.low
        current_efficiency = points / abs(bar.delta)

        # Check against high percentile threshold
        sorted_eff = sorted(efficiencies)
        threshold_idx = int(len(sorted_eff) * self.efficiency_percentile)
        threshold = sorted_eff[threshold_idx] if threshold_idx < len(sorted_eff) else 0

        # Also require substantial body relative to range
        body = abs(bar.close - bar.open)
        range_size = bar.high - bar.low
        
        # Must have both high efficiency AND large body
        has_high_efficiency = current_efficiency > threshold
        has_large_body = body > range_size * 0.65
        
        return has_high_efficiency and has_large_body

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
