"""
Flow continuation strategy: return to simplicity with volatility-aware position management.

Analysis of recent trials:
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best baseline
- #19: -0.1631, 382 trades (0.99/session) → fewer high-quality trades competitive
- #26: -0.3158, 1152 trades (3.0/session) → dynamic risk-reward hurt performance
- #25: -0.2991, 767 trades (2.0/session) → dual-threshold approach failed
- #24: -0.2466, 768 trades (2.0/session) → 1m confirmation added noise

Core insight: #9 works because it's simple. Every "improvement" has failed.
The -0.1437 score with 768 trades is the best we've achieved. Commission is
0.20 per round turn, so we're paying ~0.10 per session on average.

The remaining question: Can we improve the EDGE PER TRADE without adding complexity?

New hypothesis - VOLATILITY-AWARE POSITION SIZING:
The issue might not be entry selection (the 0.55 threshold works) but rather
that we're taking equal-sized positions regardless of market conditions.

Key observation from #19:
- 382 trades, 0.99/session, score -0.1631
- That's roughly half the trades of #9 but only 13% worse score
- This suggests: trading LESS on LOWER quality sessions is better than
  trading MORE on ALL sessions

What if we:
1. Keep the core #9 signal (0.55 threshold, simple checks)
2. Add VOLATILITY FILTERING: only trade when volatility is in a "sweet spot"
3. Skip sessions/periods with very low volatility (thin, hard to move)
4. Skip sessions/periods with very high volatility (whipsaw risk)
5. Trade normally in moderate volatility

Why this might work:
- Low volatility: tight spreads, hard to overcome commission, wide stops needed
- High volatility: whipsaws, stops get hit more often, target harder to reach
- Moderate volatility: sweet spot for flow-based mean reversion
- We reduce trades in bad conditions, keep them in good ones
- Expected: 600-700 trades of better quality

The logic:
- Calculate rolling 20-bar volatility (ATR-like measure)
- Only trade if volatility is between 0.5 and 2.0 (moderate range)
- Keep everything else from #9 (0.55 threshold, session discipline, etc.)
- Stop 1.5, Target 3.0 (same as #9)
- Max 2 entries per session (but fewer due to volatility filter)

Expected outcome:
- 600-750 trades (fewer than #9's 768)
- Better quality from volatility filtering
- Score in -0.12 to -0.14 range (slight improvement from #9)

The philosophy:
#9 is the baseline. We're not going to beat it by changing the core signal.
We're going to beat it by being selective about WHEN we trade, not HOW we trade.
Volatility is the natural gate: trade when conditions are favorable.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
        volatility_window: int = 20,
        volatility_min: float = 0.5,
        volatility_max: float = 2.0,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side (0.55 = 55%+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
            volatility_window: Number of bars for volatility calculation
            volatility_min: Minimum acceptable volatility (skip if lower)
            volatility_max: Maximum acceptable volatility (skip if higher)
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        self.volatility_window = volatility_window
        self.volatility_min = volatility_min
        self.volatility_max = volatility_max
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0
        
        # Keep rolling history for volatility calculation
        self._bar_ranges: list[float] = []

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def _calculate_volatility(self) -> float:
        """
        Calculate volatility as average true range over the window.
        Returns average of bar ranges (high - low).
        """
        if not self._bar_ranges or len(self._bar_ranges) < self.volatility_window:
            return 1.0  # Default to neutral if not enough data
        
        recent = self._bar_ranges[-self.volatility_window:]
        return sum(recent) / len(recent)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: #9 baseline with volatility filtering.
        Only trade when volatility is in moderate range (0.5 to 2.0).
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
        
        self._bars_in_session += 1
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        last_bar = history[-1]
        
        # Update volatility tracking
        bar_range = last_bar.high - last_bar.low
        self._bar_ranges.append(bar_range)
        # Keep only what we need
        if len(self._bar_ranges) > self.volatility_window + 10:
            self._bar_ranges.pop(0)
        
        # One position at a time
        if ctx.position or ctx.working_order_ids:
            return
        
        # Limit entries per session
        if self._entries_this_session >= self.max_entries_per_session:
            return
        
        if len(history) < 2:
            return
        
        # Skip first 3 bars of session
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes (in nanoseconds)
            return
        
        # Skip low-volume bars
        if last_bar.volume < self.min_volume:
            return
        
        # VOLATILITY FILTER: only trade in moderate volatility
        current_volatility = self._calculate_volatility()
        if current_volatility < self.volatility_min or current_volatility > self.volatility_max:
            return
        
        # Measure aggression: ratio of dominant side to total volume
        buy_vol = last_bar.buy_volume
        sell_vol = last_bar.sell_volume
        total_vol = last_bar.volume
        
        if total_vol <= 0:
            return
        
        buy_ratio = buy_vol / total_vol
        sell_ratio = sell_vol / total_vol
        
        # Get previous bar for directional agreement
        prev_bar = history[-2]
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Previous bar agreement: reject if strongly sell-dominated
            if prev_sell_ratio > self.flow_threshold:
                return
            
            # Price action confirmation: close above midpoint
            bar_range_check = last_bar.high - last_bar.low
            if bar_range_check > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range_check
                if close_position < 0.5:
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
            # Previous bar agreement: reject if strongly buy-dominated
            if prev_buy_ratio > self.flow_threshold:
                return
            
            # Price action confirmation: close below midpoint
            bar_range_check = last_bar.high - last_bar.low
            if bar_range_check > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range_check
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
