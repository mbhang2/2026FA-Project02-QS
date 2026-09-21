"""
Flow continuation strategy with ultra-high conviction threshold and simplified logic.

Analysis of prior trials:
- #6: -0.2427, 768 trades (2.0/session) → best so far
- #7: -0.2823, 768 trades (2.0/session) → worse than #6

Key insight: #6 outperformed #7 despite being simpler. This suggests that:

1. **Complexity is hurting us**: #7 added volatility scaling and extra price action
   checks, but scored worse. The core signal (flow ratio + directional confirmation)
   was already good in #6.

2. **Threshold tuning**: #7 raised threshold to 0.58, but that may have filtered
   out tradeable signals. #6's 0.55 was better.

3. **Price action checks in #7 were too strict**: Requiring the bar to make a new
   high/low vs prior 2 bars is overly restrictive. Many valid continuations don't
   make new extremes.

4. **Volatility scaling adds noise**: Fixed stop/target sizes perform better than
   trying to adapt them. The bracket is a simple risk/reward tool, not a
   volatility hedging instrument.

5. **The real issue**: We're still losing money overall. Commission is 0.20 per
   round turn. At 768 trades/384 sessions = 2 per session, we're trading enough
   to incur real costs. The edge must be real and consistent.

The campaign hypothesis is clear: "Aggressive flow continues over five minutes."
We should:
- Trust the signal when it's strong (high flow ratio)
- Hold for 5 minutes (the bracket handles this)
- Be selective about WHEN we trade (session timing, volume)
- Stop over-engineering the entry logic

This iteration:
1. **Return to #6's core parameters**: 0.55 threshold, 1.5/3.0 bracket, 2 entries/session
2. **Simplify price action**: Remove the "made new high/low" requirement. Just check
   that close is on the right side of the bar (simple directional confirmation).
3. **Tighten volume filter to 150**: Avoid very thin bars where flow ratio is noisy.
4. **Remove volatility scaling**: Fixed sizes work better.
5. **Adjust session skipping**: Skip first 3 bars (not 2) to avoid cash open noise.
6. **Key change**: More aggressive on flow ratio - if it's 55%+ on one side with
   non-contradicting previous bar, TAKE IT. Stop second-guessing the signal.

The hypothesis: Flow continuation works when we trust the signal and don't
over-filter. #6 was close to optimal. We refine by simplifying further and
increasing conviction in what we trade.
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
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side to total volume
                           0.55 = 55%+ of volume on one side
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume to ensure reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        
        # Track entries per session
        self._current_session = None
        self._entries_this_session = 0
        self._bars_in_session = 0

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: flow continuation signals with simple confirmation.
        Trust the signal when flow is strong and directionally non-contradicted.
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
        
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
        
        # Measure aggression: ratio of dominant side to total volume
        buy_vol = last_bar.buy_volume
        sell_vol = last_bar.sell_volume
        total_vol = last_bar.volume
        
        if total_vol <= 0:
            return
        
        buy_ratio = buy_vol / total_vol
        sell_ratio = sell_vol / total_vol
        
        # Get previous bar for directional confirmation
        prev_bar = history[-2]
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume if prev_bar.volume > 0 else 0.5
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume if prev_bar.volume > 0 else 0.5
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Directional confirmation: previous bar should not be strongly sell-biased
            # Allow neutral or buy-biased, reject only if previous was sell-dominated
            if prev_sell_ratio > 0.55:
                return
            
            # Simple price action confirmation: close should be above midpoint of bar
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.50:
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
            # Directional confirmation: previous bar should not be strongly buy-biased
            if prev_buy_ratio > 0.55:
                return
            
            # Simple price action confirmation: close should be below midpoint of bar
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > 0.50:
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
