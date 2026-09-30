"""
Flow continuation strategy: Multi-scale confirmation with adaptive exit timing.

Analysis of 42 trials reveals persistent ceiling at -0.1437. Key findings:
1. Core 0.55 flow threshold is robust and stable
2. Trade count ~768 (2.0/session) is optimal
3. Commission (0.20 per round turn) is the binding constraint
4. Single-timeframe filters haven't broken through the ceiling

Critical insight from recent trials:
- Trial #42 (volatility filter): -0.2453 (worse than baseline)
- Trial #41 (momentum filter): -0.2453 (worse than baseline)
- Trial #9 (simple opposition check): -0.1437 (still best)

The pattern is clear: complexity hurts. The baseline #9 with simple opposition
check and basic filters is the best performer. But we're still negative.

New hypothesis - TIGHTER EXIT DISCIPLINE WITH LONGER HOLDS:
The issue might not be entry quality but EXIT TIMING. Current strategy:
- Fixed 3.0 point target, 1.5 point stop (2:1 risk/reward)
- This might exit winners too early or hold losers too long

Observation: We trade 768 times per 384 sessions = 2.0 trades/session.
At 5-minute intervals with ~78 bars/session, we're entering ~2.5% of bars.
This is selective. The problem is we're not capturing the full move.

New approach - EXTEND TARGET WITH ADAPTIVE STOP:
1. Keep entry signal simple: 0.55 flow threshold + opposition check (proven)
2. Increase target to 4.0 (let winners run longer)
3. Keep stop at 1.5 (risk/reward becomes 1:2.67)
4. Add one filter: only enter if the bar shows SUSTAINED flow
   (close in upper/lower third of range, not just above/below midpoint)
5. This is stricter entry but longer hold on winners
6. Expected: 650-700 trades with better win rate
7. Score improvement to -0.12 to -0.10 range

Why this works:
- Extended target captures more of the move that flow initiates
- Stricter close position (upper/lower third not just half) = higher conviction
- Risk/reward of 1:2.67 is better than 1:2.0
- Longer hold aligns with campaign spec (5m signal, 5m+ hold)
- Simpler than multi-timeframe or volatility filters

Why we haven't tried this:
- Trial #9 used 3.0 target; we've been tweaking entry not exit
- Extending target seems risky but it's the natural next step
- The flow signal is good; we just need to let it work longer

Implementation:
1. Keep entry logic from trial #9 (opposition check, volume filter, timing)
2. Tighten price action: close must be in upper/lower THIRD (0.67 threshold)
3. Increase target to 4.0 (was 3.0)
4. Keep stop at 1.5 (unchanged)
5. Keep session tracking and max_entries_per_session at 2

Expected outcome:
- 650-700 trades (down from 768, but higher conviction)
- Better win rate from stricter entries
- Longer average hold from extended target
- Score: -0.12 to -0.10 (improvement from -0.1437)
- Better alignment with 5m+ holding period specified in campaign
"""

from __future__ import annotations


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
            target_offset: Distance for profit target (4.0 points, extended)
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

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal with stricter price action confirmation.
        Longer target to capture more of the flow-driven move.
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
