"""
Flow continuation strategy: testing reduced entry threshold with tighter confirmation.

Analysis of prior trials:
- #14: -0.3243, 926 trades (2.4/session) → adaptive sizing hurt badly
- #10/#9: -0.1437, 768 trades (2.0/session) → best so far
- #13: -0.1817, 768 trades (2.0/session) → volatility filter hurt
- #8: -0.1829, 768 trades (2.0/session) → baseline

Key insight from #14's regression:
Adaptive sizing (1 contract at 60-65%, 2 contracts at 65%+) was a disaster.
The extra size on strong signals must have introduced more losing trades than
winning ones. This suggests the relationship between flow strength and
profitability is nonlinear or that sizing up amplifies whipsaws.

Back to the drawing board:
The best results (#9/#10 at -0.1437) used:
- Flow threshold: 0.56 (56%+)
- Previous bar agreement: 0.52 (52%+)
- Price action: 0.60 threshold
- Bracket: 1.5 stop / 3.5 target
- Fixed size: 1 contract
- 2 entries per session

This is actually quite good. We're only -0.1437 away from breakeven.
Commission is 0.20 per round turn. At 768 trades, that's 153.6 index points
paid in commission. Our gross P&L must be around -0.1437 + 0.20 = 0.0563
per trade on average, which means we're actually profitable before commission.

New hypothesis - maybe the issue is ENTRY TIMING within the bar:
We enter on bar close, but the bar that just closed might be mean-reverting.
What if we're catching the tail end of moves that reverse immediately?

This iteration tests:
**Stricter entry confirmation: require the NEXT bar to also show agreement.**

Instead of entering immediately on a 0.56+ flow bar:
1. Detect the strong flow bar (0.56+ on one side)
2. Check previous bar agreement (0.52 rule)
3. Check price action (0.60 rule)
4. **NEW**: Wait for the next bar to also show directional agreement (>0.52)
5. Only then enter on the second bar's close

This adds one bar of latency (5 minutes) but filters out:
- Single-bar spikes that reverse
- Bars that are outliers in their local context
- Whipsaw entries that get stopped immediately

The tradeoff:
- Fewer entries (maybe 500-600 instead of 768)
- Higher quality entries (both bars agree on direction)
- Possibly higher edge per trade
- Net score could be better despite fewer trades

Risk: If we miss the move because we wait one bar, we might not capture enough
of the 5-minute continuation. But the campaign spec says "position held at least
5 minutes", not "must enter immediately". Entering on bar 2 and holding to bar 7
is still a valid 5-minute hold.

Actually, let me reconsider. The campaign spec is clear: "Signal measured over
5 minutes. Position held at least 5 minutes." This doesn't prohibit waiting for
confirmation; it just requires we hold for 5 minutes once we enter.

But there's a simpler insight: maybe we're entering on bars that are about to
mean-revert. What if instead of waiting for the next bar, we just require the
current bar's close to be STRONGER (in the upper/lower THIRD, not just 60%)?

This iteration actually tests:
**Tighter price action confirmation: close must be in upper/lower THIRD (0.67).**

This filters out bars that:
- Have weak closes despite strong flow
- Are likely to reverse intra-bar
- Don't show conviction at the close

Combined with the existing filters, this should give us:
- Fewer, higher-quality entries
- Better edge per trade
- Possibly better overall score

Parameters:
- Flow threshold: 0.56 (unchanged)
- Previous bar agreement: 0.52 (unchanged)
- Price action: 0.67 threshold (tighter, was 0.60)
- Bracket: 1.5 stop / 3.5 target (unchanged)
- Fixed size: 1 contract (unchanged)
- 2 entries per session (unchanged)

Expected result: Trade count drops to ~500-600, but edge improves. Net score
better than -0.1437 because we're filtering out the weakest signals.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.56,
        prev_agreement_threshold: float = 0.52,
        close_threshold: float = 0.67,
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
            prev_agreement_threshold: Previous bar should show agreement (0.52+)
            close_threshold: Close position in range threshold (0.67 = upper/lower third)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.5 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.prev_agreement_threshold = prev_agreement_threshold
        self.close_threshold = close_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: flow continuation with tight price action confirmation.
        Require close in upper/lower third of bar, not just 60%.
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
            
            # Tight price action confirmation: close must be in UPPER THIRD of bar
            # (0.67 threshold instead of 0.60). This filters out weak closes.
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < self.close_threshold:
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
            
            # Tight price action confirmation: close must be in LOWER THIRD of bar
            # (1 - 0.67 = 0.33 threshold). This filters out weak closes.
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position > (1.0 - self.close_threshold):
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
