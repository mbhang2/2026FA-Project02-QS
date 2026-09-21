"""
Flow continuation strategy: Adaptive entry timing with volatility-adjusted thresholds.

Analysis of 41 trials reveals a persistent ceiling around -0.1437. Key patterns:
1. The 0.55 flow threshold is robust and stable across variations
2. Trade count of ~768 (2.0/session) is optimal
3. Commission (0.20 per round turn) is the binding constraint
4. Multi-timeframe and momentum filters haven't broken through

Critical insight from trial #41:
The momentum filter (session cumulative delta) showed promise in theory but
likely over-filtered entries. The real issue is ENTRY QUALITY, not quantity.

New hypothesis - VOLATILITY-ADJUSTED ENTRY WITH STRICTER CONFIRMATION:
Instead of just checking opposition and momentum, we need to:
1. Keep the core 5m bar 0.55 flow threshold (proven)
2. Measure recent volatility (ATR-like metric over last 10 bars)
3. Only enter when:
   - Current bar shows strong flow (0.55+)
   - Previous bar does NOT show opposition (0.55+)
   - Close is on the correct side of the bar (upper/lower half)
   - Volatility is not at extremes (too high = noise, too low = no follow-through)
4. This filters out both whipsaws (high vol) and dead zones (low vol)
5. Expected: 650-750 trades with better win rate
6. Score improvement to -0.12 to -0.10 range

Why this works:
- High volatility = noise and wider stops = worse risk/reward
- Low volatility = no follow-through on signals = stops hit
- Sweet spot = moderate volatility where signal has room to move
- This is NOT over-fitting: volatility is a real market condition
- Reduces commission impact by improving win rate

Why we haven't tried this:
- Requires tracking volatility state (simple max-min over window)
- But it's orthogonal to flow signal, not competing with it
- Natural evolution: we've proven flow works, now filter by conditions

Implementation:
1. Track high/low prices over last 10 bars for volatility metric
2. Calculate ATR-like measure: average(high-low) over window
3. Only enter if current ATR is in middle 50% of recent range
4. Keep all other logic from trial #9 (opposition check, volume filter, timing)
5. Expected trade count: 700-750 (natural reduction from volatility filter)

Expected outcome:
- 700-750 trades (down from 768, but higher conviction)
- Better win rate from volatility filtering
- Score: -0.12 to -0.10 (improvement from -0.1437)
- More robust across different market regimes
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
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
        volatility_window: int = 10,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side (0.55)
            opposition_threshold: Reject if prev bar opposed at this level (0.55)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
            volatility_window: Number of bars to measure volatility over
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.opposition_threshold = opposition_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        self.volatility_window = volatility_window
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0
        
        # Track bar ranges for volatility calculation
        self._bar_ranges: deque = deque(maxlen=volatility_window)

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def _get_volatility_percentile(self) -> float | None:
        """
        Calculate where current volatility sits in recent history.
        Returns percentile (0-1) or None if not enough data.
        If None, we return early and don't trade (conservative).
        """
        if len(self._bar_ranges) < self.volatility_window:
            return None
        
        ranges = list(self._bar_ranges)
        ranges_sorted = sorted(ranges)
        
        # Find where the most recent range sits
        current_range = ranges[-1]
        
        # Count how many historical ranges are below current
        below = sum(1 for r in ranges_sorted if r < current_range)
        percentile = below / len(ranges_sorted)
        
        return percentile

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal with volatility-adjusted entry.
        Only enter when signal is strong AND volatility is in sweet spot.
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
            self._bar_ranges.clear()
        
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
        
        # Track this bar's range for volatility
        bar_range = last_bar.high - last_bar.low
        self._bar_ranges.append(bar_range)
        
        # Skip first 3 bars of session (cash open microstructure)
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes
            return
        
        # Skip low-volume bars
        if last_bar.volume < self.min_volume:
            return
        
        # Volatility filter: only trade when volatility is in middle 50%
        # (not in top 25% noise, not in bottom 25% dead zone)
        vol_percentile = self._get_volatility_percentile()
        if vol_percentile is None:
            return  # Not enough history yet
        
        if vol_percentile < 0.25 or vol_percentile > 0.75:
            # Volatility is either too low or too high for this signal
            return
        
        # Measure flow ratios
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
        
        # Price action confirmation: close on correct side of midpoint
        if bar_range <= 0:
            return
        
        close_position = (last_bar.close - last_bar.low) / bar_range
        
        # Strong buy signal on 5m
        if buy_ratio > self.flow_threshold:
            # Opposition check: previous bar shouldn't be strongly sold
            if prev_sell_ratio > self.opposition_threshold:
                return
            
            # Price action confirmation: close above midpoint
            if close_position < 0.5:
                return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal on 5m
        elif sell_ratio > self.flow_threshold:
            # Opposition check: previous bar shouldn't be strongly bought
            if prev_buy_ratio > self.opposition_threshold:
                return
            
            # Price action confirmation: close below midpoint
            if close_position > 0.5:
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
