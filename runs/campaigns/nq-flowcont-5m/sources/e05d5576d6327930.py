"""
Flow continuation strategy: testing dynamic thresholds based on session volatility.

Analysis of prior trials:
- #12: -0.2181, 767 trades (2.0/session) → regression from #10/#9
- #10: -0.1437, 768 trades (2.0/session) → tied for best
- #9: -0.1437, 768 trades (2.0/session) → tied for best
- #8: -0.1829, 768 trades (2.0/session) → previous baseline

Key insight from recent regressions:
The stricter persistence threshold (0.54 vs 0.52) and tighter close positioning
(0.50) in #12 actually hurt performance. This suggests we were over-filtering
and missing legitimate continuation signals.

Going back to what worked (#9/#10 at -0.1437):
- Flow threshold: 0.56 (56%+)
- Previous bar agreement: 0.52 (52%+)
- Price action: 0.60 threshold (upper/lower 60% of range)
- Bracket: 1.5 stop / 3.5 target
- 2 entries per session

The problem is still clear: we're negative by 0.1437, which is substantial.
Commission is 0.20 per round turn. At 2 trades/session across 384 sessions,
that's 768 trades, paying ~153.6 index points total in commission, or ~0.20
per trade. We need an edge of at least 0.25 per trade to break even.

New hypothesis - the real issue might be WHEN we're trading:
1. Are we trading more in quiet sessions (low volatility) where edges are smaller?
2. Are we trading in specific times of day that are worse?
3. Should we be MORE selective about session conditions, not bar conditions?

This iteration tests a simpler approach:
**Revert to the #9/#10 parameters that worked, but add session volatility gating.**

Measure the recent volatility of the session (last 10 bars' average range).
Only trade when volatility is above the median for that session so far.
This filters out the quiet periods where our edge disappears into noise.

Additional refinement:
- Keep the core parameters from #9 (threshold 0.56, prev 0.52, close 0.60)
- Add volatility filter: only enter if current bar range > median of last 10 bars
- This should naturally reduce entries in quiet periods
- Keep 2 entries/session but they'll be more selective

The logic: Flow continuation works best when there's enough volatility to
actually move. In quiet periods, even strong flow signals don't generate
meaningful moves. By gating on volatility, we trade only when conditions
support continuation.

Risk: We might trade less overall (maybe 500-600 trades). But those trades
should have much higher edge because we're avoiding the low-volatility trap.
"""

from __future__ import annotations
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.56,
        prev_agreement_threshold: float = 0.52,
        stop_offset: float = 1.5,
        target_offset: float = 3.5,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
        volatility_window: int = 10,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side (0.56 = 56%+)
            prev_agreement_threshold: Previous bar should show agreement (0.52+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.5 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
            volatility_window: Number of bars for volatility calculation
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.prev_agreement_threshold = prev_agreement_threshold
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
        self._session_bar_ranges: deque[float] = deque(maxlen=volatility_window)

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: flow continuation with volatility gating.
        Only enter when flow signal is strong AND volatility supports continuation.
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
            self._session_bar_ranges.clear()
        
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
        
        # Track bar range for volatility calculation
        bar_range = last_bar.high - last_bar.low
        self._session_bar_ranges.append(bar_range)
        
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
        
        # Volatility filter: only trade when current bar range exceeds the median
        # of recent bars. This avoids trading in quiet periods where edge is lost.
        if len(self._session_bar_ranges) >= self.volatility_window:
            ranges_sorted = sorted(self._session_bar_ranges)
            median_range = ranges_sorted[len(ranges_sorted) // 2]
            if bar_range <= median_range:
                # Current bar is below median volatility - skip this signal
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
        
        # Strong buy signal: current bar has 56%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Previous bar agreement: should show some buying (>0.52) or at least
            # not show strong selling (<=0.55).
            if prev_sell_ratio > 0.55:
                return
            
            # Price action confirmation: close must be in upper 60% of bar
            bar_range_val = last_bar.high - last_bar.low
            if bar_range_val > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range_val
                if close_position < 0.60:
                    return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal: current bar has 56%+ sell volume
        elif sell_ratio > self.flow_threshold:
            # Previous bar agreement: should show some selling (>0.52) or at least
            # not show strong buying (<=0.55).
            if prev_buy_ratio > 0.55:
                return
            
            # Price action confirmation: close must be in lower 40% of bar
            bar_range_val = last_bar.high - last_bar.low
            if bar_range_val > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range_val
                if close_position > 0.40:
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
