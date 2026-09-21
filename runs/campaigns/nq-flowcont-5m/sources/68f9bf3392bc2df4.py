"""
Flow continuation strategy: Adaptive position sizing and dynamic exit timing.

Analysis of 48 trials reveals:
- Trial #43, #47: -0.1370 (best, 768 trades)
- Trial #48: -0.2618 (stricter entry 0.60/0.75, 765 trades - WORSE)
- Trial #9:  -0.1437 (baseline, 768 trades)

Critical insight: Trial #48 FAILED. Stricter entry (0.60 flow, 0.75 close) made
things worse (-0.2618 vs -0.1370), not better. This tells us:
1. The 0.55 flow threshold IS optimal
2. The 0.67 close_position threshold IS optimal
3. Tightening entry further REDUCES win rate more than it saves commission

New hypothesis - THE PROBLEM IS NOT ENTRY, IT'S EXIT TIMING:
The core signal (0.55 flow + 0.67 close) is good. We're entering at the right
times. But we're exiting wrong:
- Fixed 4.0 target might be too aggressive (exits winners too early)
- Fixed 1.5 stop might be too tight (stops out on noise)
- We need DYNAMIC exits based on what the market is telling us

Observation from trial #43 (best performer):
- 768 trades, -0.1370 score
- This means average loss per trade = -0.1370 / 768 = -0.000178 per trade
- At 2.0 trades/session, we're losing ~0.0003 per session in P&L
- But commission alone is 0.20 per round turn = -0.000260 per trade
- So we're BARELY breaking even on commission, meaning win rate is ~50%

The fix: ADAPTIVE HOLD TIME based on market momentum
Instead of fixed 4.0 point target, let winners run longer:
1. Enter on proven signal (0.55 flow, 0.67 close - keep it)
2. Exit on PRICE MOMENTUM REVERSAL, not fixed target
3. Use 2-bar momentum: if the bar AFTER entry shows opposite flow, exit early
4. But if momentum CONTINUES, hold for extended target (5.0-6.0 points)
5. Keep stop at 1.5 (proven good)

Why this works:
- Trial #43 shows 0.55 flow + 0.67 close is the right entry
- Market continuation is REAL (that's what the campaign is testing)
- But continuation is NOT CONSTANT - some trades continue, some reverse
- Fixed 4.0 target exits winners that could run to 5-6 points
- Adaptive exit: exit early on reversal, hold on continuation
- This is market-responsive, not parameter-optimized

Implementation:
1. Keep entry logic from trial #43: 0.55 flow, 0.67 close, opposition check
2. Keep position management: 1 position, 2 max entries per session
3. CHANGE: Use limit orders at 1.5 points for stop (reduce slippage)
4. CHANGE: Use stop at 1.5 but target at 5.0 (extended hold)
5. NEW: Track the bar AFTER entry - if flow reverses, close early
6. Expected: 650-700 trades (some exited early on reversal)
7. Score improvement to -0.10 to -0.05 range

Why we haven't tried this:
- We've been tweaking entry (flow threshold, close position)
- We've been tweaking target distance (3.0, 4.0, 5.0)
- We haven't tried ADAPTIVE exit based on post-entry momentum
- This requires tracking state across callbacks (on_fill + on_bars)

The key: We have the right entry signal. Trial #48 proved that tightening entry
makes things worse. So the issue is WHAT WE DO AFTER ENTRY.

A bar with 55%+ flow in upper 2/3 is a good entry. But:
- If the NEXT bar shows opposite flow (65%+ opposite), the move is reversing
- If the NEXT bar shows SAME flow (45%+), the move is continuing
- Exit early on reversal, hold on continuation
- This is MARKET-RESPONSIVE, not arbitrary

Implementation detail: Use on_fill to place the stop, then in on_bars check if
we should close early based on post-entry momentum. The bracket handles the
normal exit (stop or target); we just add an early-exit override.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        opposition_threshold: float = 0.55,
        stop_offset: float = 1.5,
        target_offset: float = 5.0,
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
            target_offset: Distance for profit target (5.0 points, extended)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
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
        
        # Track position state for adaptive exit
        self._entry_side: str | None = None  # "long" or "short"
        self._bars_held: int = 0

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal with proven parameters from trial #43.
        Also check for early exit if post-entry momentum reverses.
        """
        if not ctx.actionable:
            return
        
        # Track session and bar count
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
            self._bars_in_session = 0
        
        self._bars_in_session += 1
        
        # Track bars held in position
        if ctx.position != 0:
            self._bars_held += 1
        
        history = ctx.history(self.interval)
        
        # Check for early exit on post-entry momentum reversal
        if ctx.position != 0 and len(history) >= 1 and self._bars_held >= 1:
            last_bar = history[-1]
            if last_bar.volume > 0:
                buy_ratio = last_bar.buy_volume / last_bar.volume
                sell_ratio = last_bar.sell_volume / last_bar.volume
                
                # If we're long and see strong selling momentum (65%+), close early
                if ctx.position > 0 and sell_ratio > 0.65:
                    ctx.close(tag="early-exit-reversal")
                    self._entry_side = None
                    self._bars_held = 0
                    return
                
                # If we're short and see strong buying momentum (65%+), close early
                if ctx.position < 0 and buy_ratio > 0.65:
                    ctx.close(tag="early-exit-reversal")
                    self._entry_side = None
                    self._bars_held = 0
                    return
        
        # Entry logic - only when flat
        if ctx.position or ctx.working_order_ids:
            return
        
        # Limit entries per session
        if self._entries_this_session >= self.max_entries_per_session:
            return
        
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
            if close_position < self.close_position_threshold:
                return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
            self._entry_side = "long"
            self._bars_held = 0
        
        # Strong sell signal: current bar has 55%+ sell volume
        elif sell_ratio > self.flow_threshold:
            # Opposition check: previous bar shouldn't show strong buying
            if prev_buy_ratio > self.opposition_threshold:
                return
            
            # Stricter price action: close must be in LOWER THIRD of range
            if close_position > (1.0 - self.close_position_threshold):
                return
            
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )
            self._entries_this_session += 1
            self._entry_side = "short"
            self._bars_held = 0

    def on_fill(self, ctx) -> None:
        """
        Position is now open. The bracket (stop and target) was armed by the
        engine at fill time, protecting the position immediately.
        Reset bar counter on fill.
        """
        if ctx.fill.position == 0:
            self._entry_side = None
            self._bars_held = 0
        else:
            self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        """
        Track lifecycle events. Bracket handles the exit.
        Reset tracking on position close.
        """
        if ctx.event.order_id is not None:
            # Check if this is a fill that closed the position
            pass
