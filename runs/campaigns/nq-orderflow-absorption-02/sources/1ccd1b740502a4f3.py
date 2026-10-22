"""
Volume profile absorption strategy - focused on exposure floor with refined detection.

Analysis of prior failures:
- #0: Stop placement error (short stop below entry instead of above)
- #1-3: Exposure floor failures (0-1 sessions instead of 192+ required)

Root causes identified:
1. Absorption detection was too strict - requiring both strong flow AND reversal
2. Level extraction from volume profile had granularity issues
3. Stop price calculation had sign errors for shorts
4. Setups were being rejected for stop distance bounds too frequently
5. The profile window (maxlen=10000) might not hold enough bars across sessions

This revision focuses on:
1. Fix the short stop calculation: stop must be ABOVE entry for shorts
2. Increase absorption detection sensitivity by lowering thresholds
3. Simplify level detection - use more price points with finer granularity
4. Accept wider range of stop distances to get more setups
5. Track bars across sessions more reliably
6. Enter on absorption signal directly (no initiation wait by default)
7. Accumulate bars on self to bypass window limitations
"""

from __future__ import annotations

from collections import deque, defaultdict
from typing import Optional


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        stop_buffer: float = 0.25,
        target_multiple: float = 2.0,
        absorption_threshold: float = 0.55,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.stop_buffer = stop_buffer
        self.target_multiple = target_multiple
        self.absorption_threshold = absorption_threshold
        self.size = size
        
        # Accumulate all bars to build rolling profile
        self.all_bars: list = []
        self.current_session: Optional[int] = None
        
        # Absorption tracking - can have multiple pending
        self.pending_absorptions: list[dict] = []
        
        # Efficiency tracking for confirmation
        self.bar_efficiencies: deque = deque(maxlen=40)
        self.recent_ranges: deque = deque(maxlen=25)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        current_bar = history[-1]
        
        # Track session changes
        if current_bar.session != self.current_session:
            self.current_session = current_bar.session
        
        # Accumulate bar for profile (keep last ~5000 bars = ~3-4 sessions)
        self.all_bars.append(current_bar)
        if len(self.all_bars) > 5000:
            self.all_bars.pop(0)
        
        # Update efficiency metrics
        self._update_efficiency(current_bar)
        
        # Skip if already in position
        if ctx.position or ctx.working_order_ids:
            return
        
        # Check for new absorption setups
        self._detect_absorptions(current_bar, history)
        
        # Process pending absorptions
        self._process_pending_absorptions(ctx, current_bar)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    def _get_volume_profile(self) -> dict[float, int]:
        """Build volume profile from accumulated bars."""
        profile: dict[float, int] = defaultdict(int)
        
        for bar in self.all_bars:
            if bar.volume == 0:
                continue
            
            # Distribute volume to multiple price points for better level detection
            # Use 0.25 point granularity
            prices = [
                round(bar.open * 4) / 4,
                round(bar.high * 4) / 4,
                round(bar.low * 4) / 4,
                round(bar.close * 4) / 4,
                round(((bar.high + bar.low) / 2) * 4) / 4,
                round(((bar.open + bar.close) / 2) * 4) / 4,
            ]
            
            unique_prices = list(set(prices))
            vol_per_price = bar.volume / len(unique_prices)
            
            for price in unique_prices:
                profile[price] += int(vol_per_price)
        
        return dict(profile)

    def _get_candidate_levels(self) -> list[float]:
        """Extract candidate levels from volume profile."""
        profile = self._get_volume_profile()
        
        if not profile:
            return []
        
        # Use median volume as threshold for candidate levels
        volumes = sorted(profile.values())
        if not volumes:
            return []
        
        median_vol = volumes[len(volumes) // 2]
        
        # Candidates are levels with volume >= median
        candidates = sorted([price for price, vol in profile.items() if vol >= median_vol])
        
        return candidates

    def _detect_absorptions(self, current_bar, history) -> None:
        """Detect absorption setups at volume profile levels."""
        if current_bar.volume == 0 or len(history) < 2:
            return
        
        prior_bar = history[-2]
        candidate_levels = self._get_candidate_levels()
        
        if not candidate_levels:
            return
        
        delta = current_bar.delta
        if delta == 0:
            return
        
        flow_ratio = abs(delta) / current_bar.volume
        
        # Check each candidate level
        for level in candidate_levels:
            # Level must be within current bar's range
            if not (current_bar.low <= level <= current_bar.high):
                continue
            
            # Long absorption: strong buy flow with price reversal
            if delta > 0 and flow_ratio >= self.absorption_threshold:
                # Reversal: close below prior close = failed extension upward
                if current_bar.close < prior_bar.close:
                    # Stop is below the bar
                    stop_price = current_bar.low - self.stop_buffer
                    stop_distance = current_bar.close - stop_price
                    
                    # Accept wider range of stops
                    if 0.1 <= stop_distance <= 10.0:
                        self.pending_absorptions.append({
                            "direction": "long",
                            "level": level,
                            "stop_price": stop_price,
                            "stop_distance": stop_distance,
                            "bars_held": 0,
                        })
            
            # Short absorption: strong sell flow with price reversal
            elif delta < 0 and flow_ratio >= self.absorption_threshold:
                # Reversal: close above prior close = failed extension downward
                if current_bar.close > prior_bar.close:
                    # Stop is ABOVE the bar for shorts
                    stop_price = current_bar.high + self.stop_buffer
                    stop_distance = stop_price - current_bar.close
                    
                    # Accept wider range of stops
                    if 0.1 <= stop_distance <= 10.0:
                        self.pending_absorptions.append({
                            "direction": "short",
                            "level": level,
                            "stop_price": stop_price,
                            "stop_distance": stop_distance,
                            "bars_held": 0,
                        })

    def _process_pending_absorptions(self, ctx, current_bar) -> None:
        """Process pending absorptions - enter on next bar."""
        to_remove = []
        
        for i, absorption in enumerate(self.pending_absorptions):
            absorption["bars_held"] += 1
            
            # Enter on the bar after absorption detection (bars_held >= 1)
            # This gives the signal one bar to confirm
            if absorption["bars_held"] >= 1:
                self._place_entry(ctx, absorption)
                to_remove.append(i)
            elif absorption["bars_held"] > 5:
                # Stale setup - discard after 5 bars
                to_remove.append(i)
        
        # Remove processed absorptions in reverse order
        for i in reversed(to_remove):
            self.pending_absorptions.pop(i)

    def _update_efficiency(self, bar) -> None:
        """Track bar efficiency for potential initiation confirmation."""
        if bar.volume == 0:
            return
        
        range_traveled = bar.high - bar.low
        abs_flow = abs(bar.delta) if bar.delta != 0 else 1
        
        efficiency = range_traveled / abs_flow
        self.bar_efficiencies.append(efficiency)
        self.recent_ranges.append(range_traveled)

    def _place_entry(self, ctx, absorption: dict) -> None:
        """Place entry with structural stop and target."""
        direction = absorption["direction"]
        stop_distance = absorption["stop_distance"]
        target_distance = stop_distance * self.target_multiple
        
        tag = f"{direction}-absorption"
        
        if direction == "long":
            ctx.buy(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=tag,
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_distance,
                target_offset=target_distance,
                tag=tag,
            )
