"""
Flow continuation strategy: Asymmetric risk/reward with adaptive volume normalization.

Analysis of 45 trials reveals critical pattern:
- Trial #43: -0.1370 (close_position_threshold=0.67, target=4.0, 768 trades)
- Trial #45: -0.1692 (close_position_threshold=0.75, target=5.0, 768 trades)
- Trial #9:  -0.1437 (baseline: target=3.0, 768 trades)

Key insight: Trial #43 beat #45, which beat #9. This shows:
1. Extended targets help (4.0 > 3.0)
2. But 5.0 is too extended (worse than 4.0)
3. Stricter entries help (0.67 > baseline)
4. But 0.75 is too strict (worse than 0.67)

The pattern suggests an OPTIMAL SWEET SPOT, not monotonic improvement.

New hypothesis - VOLUME-NORMALIZED FLOW INTENSITY:
The 0.55 threshold treats all bars equally, but market conditions vary:
- A 55% ratio on 500 contracts is different from 55% on 50 contracts
- High-volume bars with 55%+ flow are stronger signals than low-volume bars
- We should WEIGHT the threshold based on volume intensity

Current best (trial #43):
- 768 trades, -0.1370 score
- 0.55 flow threshold (binary gate)
- 0.67 close_position_threshold (stricter)
- 4.0 target, 1.5 stop
- min_volume=150 (absolute floor)

The problem: We're not distinguishing between:
- A 500-contract bar with 55% buy flow (strong signal)
- A 150-contract bar with 55% buy flow (weak signal)

New approach - ADAPTIVE FLOW THRESHOLD BASED ON VOLUME:
1. Keep base 0.55 flow threshold
2. Add VOLUME INTENSITY check: only enter if bar volume is above session median
3. This filters for naturally high-volume bars (strong conviction periods)
4. High-volume bars with 55%+ flow are much more reliable
5. Expected: 500-600 trades (filtered for quality)
6. Score improvement to -0.12 to -0.10 range

Why this works:
- Trial #43 was best with 768 trades; we're not trying to reduce count
- But we can improve QUALITY by filtering for high-volume bars
- Session median volume naturally adapts to market regime
- Removes trades on thin bars where flow ratio is noisy
- Commission impact: fewer total trades, but better win rate
- Simpler than multi-bar confirmation; orthogonal to previous attempts

Why we haven't tried this:
- We've been tweaking entry thresholds (0.55, 0.67, 0.75) not volume filtering
- Volume normalization is natural but wasn't in the iteration path
- Trial #43 used min_volume=150 (absolute) not relative (session-based)

Implementation:
1. In setup(), track volume statistics during warmup
2. Calculate session median volume
3. Only enter if:
   - Flow ratio > 0.55 (proven threshold)
   - Bar volume > session median (high-volume bar)
   - close_position in range [0.67, 0.33] (proven stricter entry)
   - Opposition check (previous bar shouldn't oppose)
4. Keep target=4.0, stop=1.5 (proven from trial #43)
5. Keep max_entries_per_session=2

Expected outcome:
- 500-600 trades (filtered for high-volume bars)
- Better win rate from volume-based filtering
- Improved signal quality without reducing total count too much
- Score: -0.12 to -0.10 (improvement from -0.1370)
- More robust across market regimes (adapts to session volume)
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        opposition_threshold: float = 0.55,
        stop_offset: float = 1.5,
        target_offset: float = 4.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
        close_position_threshold: float = 0.67,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side (0.55 = 55%+)
            opposition_threshold: Reject if prev bar opposed at this level (0.55)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (4.0 points)
            size: Position size in contracts
            min_volume: Minimum absolute bar volume
            max_entries_per_session: Maximum entries per session
            close_position_threshold: Close must be in outer third (0.67)
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.opposition_threshold = opposition_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        self.close_position_threshold = close_position_threshold
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0
        
        # Track session volumes for adaptive filtering
        self._session_volumes: deque = deque(maxlen=512)
        self._session_median_volume: float = 200.0  # Default fallback

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars and analyze training data."""
        ctx.subscribe(self.interval)
        
        # Analyze training data to get typical volume distribution
        train_bars = ctx.train_bars(self.interval)
        if len(train_bars) > 0:
            volumes = sorted([b.volume for b in train_bars if b.volume > 0])
            if len(volumes) > 0:
                # Use median of training volumes as baseline
                median_idx = len(volumes) // 2
                self._session_median_volume = float(volumes[median_idx])
                # Ensure minimum is reasonable
                if self._session_median_volume < 100:
                    self._session_median_volume = 200.0

    def _calculate_session_median(self) -> float:
        """Calculate median volume for current session."""
        if len(self._session_volumes) < 5:
            return self._session_median_volume  # Use training median if not enough data
        
        sorted_vols = sorted(self._session_volumes)
        idx = len(sorted_vols) // 2
        return float(sorted_vols[idx])

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal with volume-normalized filtering.
        Only enter on high-volume bars with strong flow and strict price action.
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
            self._session_volumes.clear()
        
        self._bars_in_session += 1
        
        # One position at a time
        if ctx.position or ctx.working_order_ids:
            return
        
        # Limit entries per session
        if self._entries_this_session >= self.max_entries_per_session:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 2:
            return
        
        last_bar = history[-1]  # Bar that just closed
        
        # Track volume for this session
        if last_bar.volume > 0:
            self._session_volumes.append(last_bar.volume)
        
        # Skip first 3 bars of session (cash open microstructure is different)
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session (forced liquidation, end-of-day pressure)
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes (in nanoseconds)
            return
        
        # Skip low-volume bars (flow ratio unreliable on thin bars)
        if last_bar.volume < self.min_volume:
            return
        
        # NEW: Skip bars below session median (filter for high-volume periods)
        session_median = self._calculate_session_median()
        if last_bar.volume < session_median:
            return
        
        # Measure aggression: ratio of dominant side to total volume
        buy_vol = last_bar.buy_volume
        sell_vol = last_bar.sell_volume
        total_vol = last_bar.volume
        
        if total_vol <= 0:
            return
        
        buy_ratio = buy_vol / total_vol
        sell_ratio = sell_vol / total_vol
        
        # Get previous bar for opposition check
        prev_bar = history[-2]
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Price action confirmation: close must be in outer third of range
        bar_range = last_bar.high - last_bar.low
        if bar_range <= 0:
            return
        
        close_position = (last_bar.close - last_bar.low) / bar_range
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Opposition check: previous bar shouldn't show strong selling
            if prev_sell_ratio > self.opposition_threshold:
                return
            
            # Stricter price action: close must be in UPPER THIRD of range
            # (not just above midpoint). Requires sustained buying pressure.
            if close_position < self.close_position_threshold:
                return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal: current bar has 55%+ sell volume
        elif sell_ratio > self.flow_threshold:
            # Opposition check: previous bar shouldn't show strong buying
            if prev_buy_ratio > self.opposition_threshold:
                return
            
            # Stricter price action: close must be in LOWER THIRD of range
            # (not just below midpoint). Requires sustained selling pressure.
            if close_position > (1.0 - self.close_position_threshold):
                return
            
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )
            self._entries_this_session += 1

    def on_fill(self, ctx) -> None:
        """
        Position is now open. The bracket (stop and target) was armed by the
        engine at fill time, protecting the position immediately.
        """
        pass

    def on_lifecycle(self, ctx) -> None:
        """
        Track lifecycle events. Bracket handles the exit.
        """
        pass
