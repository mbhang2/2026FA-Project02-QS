"""
Flow continuation strategy: refined threshold with persistent session tracking.

Analysis of prior trials:
- #8: -0.1829, 768 trades (2.0/session) → best so far
- #7: -0.2823, 768 trades (2.0/session) → complexity hurt performance
- #6: -0.2427, 768 trades (2.0/session) → good baseline

Key insights from #8's success:
1. **Simplicity wins**: Removing volatility scaling and strict price action checks
   improved the score. The core flow signal is strong enough on its own.

2. **The threshold sweet spot**: 0.55 (55%+ on one side) is better than 0.58.
   We're catching the right signals without over-filtering.

3. **Session discipline matters**: Skipping first 3 bars and last 5 minutes helps
   avoid microstructure noise and end-of-day chaos.

4. **Volume filter at 150 works**: Thin bars have unreliable flow ratios.

5. **The remaining edge**: We're still negative overall (-0.1829), but much better
   than earlier attempts. Commission is 0.20 per round turn. At 2 trades/session
   across 384 sessions, we're paying ~0.10 in commission per session on average.
   The issue: our edge per trade is too small or inconsistent.

What might improve #8:
1. **Entry selectivity**: Maybe we're entering on borderline signals. Raise the
   flow threshold slightly to 0.56-0.57 to get only the highest conviction.

2. **Previous bar logic**: Current logic rejects if prev bar was opposite direction
   at 0.55+. Maybe we need STRONGER confirmation - prev bar should also show
   some agreement (>0.52+) rather than just not be opposite.

3. **Holding period**: The 5-minute bracket is fixed. What if we're exiting too
   early on some moves? Keep the bracket but maybe the target is too tight.
   Try 3.5 instead of 3.0.

4. **Session distribution**: Are we trading too much in certain sessions? Maybe
   we should be more selective about WHICH sessions we trade in, not just WHEN
   within a session.

5. **Price action confirmation**: The "close in upper/lower half" check is very
   loose. Maybe require close to be in upper/lower THIRD (0.67 threshold) for
   stronger confirmation.

6. **Bar persistence**: What if we're being whipsawed by single bars? Require
   the flow signal to persist: if we see a 0.55+ buy bar, only enter if the
   NEXT bar (if we're still in time) also shows buying pressure.

This iteration focuses on:
- Raise flow threshold to 0.56 (slightly higher conviction)
- Require previous bar to show some agreement (>0.52 on same side) not just
  lack of opposition
- Tighten price action: close must be in upper/lower THIRD of range (0.67)
- Increase target to 3.5 (let winners run a bit longer)
- Keep stop at 1.5 (risk/reward becomes 1:2.33)
- Keep everything else from #8 (session tracking, volume filter, timing)

The hypothesis: The core signal is sound, but we need higher conviction entries.
By requiring slightly more agreement from the previous bar and tighter price
action confirmation, we reduce whipsaws and improve the edge.
"""

from __future__ import annotations


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
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side (0.56 = 56%+)
            prev_agreement_threshold: Previous bar should show some agreement (0.52+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.5 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.prev_agreement_threshold = prev_agreement_threshold
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
        Entry logic: higher conviction flow signals with previous bar agreement.
        Only enter when current bar shows strong flow AND previous bar shows
        some agreement (not necessarily strong, but not opposed).
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
        
        # Get previous bar for directional agreement
        prev_bar = history[-2]
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume if prev_bar.volume > 0 else 0.5
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume if prev_bar.volume > 0 else 0.5
        
        # Strong buy signal: current bar has 56%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Previous bar agreement: should show some buying (>0.52) or at least
            # not show strong selling (<=0.55). Don't enter if prev was sell-dominated.
            if prev_sell_ratio > 0.55:
                return
            
            # Stronger price action confirmation: close must be in UPPER THIRD of bar
            # (not just above midpoint). This filters out bars that close high on
            # a narrow range.
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.67:  # Require upper third, not just upper half
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
            
            # Stronger price action confirmation: close must be in LOWER THIRD of bar
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > 0.33:  # Require lower third, not just lower half
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
