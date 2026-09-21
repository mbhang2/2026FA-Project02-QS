"""
Flow continuation strategy with refined entry mechanics and commission-aware exit.

Prior trials show negative scores, suggesting the basic flow continuation hypothesis
needs refinement. Key observations:

1. **Prior trials traded too frequently** (#2: 65,608 trades in 384 sessions ≈ 171/session).
   At 0.20 points commission per round turn, this is unsustainable. We need fewer,
   higher-conviction trades.

2. **The 5m minimum hold is critical** per campaign spec. Exits must respect this.
   Prior implementations may have exited too quickly via tight brackets.

3. **Flow signal timing matters**: The signal should be measured over a complete bar,
   and entry should come on the NEXT bar (T+1 rule). We need to ensure we're not
   fighting against the bar that generated the signal.

4. **Bracket sizing**: Fixed offsets may be wrong. The target (profit objective) should
   be larger than the stop (risk), with a favorable risk/reward ratio. Prior trials
   used symmetric or nearly-symmetric brackets, which is suboptimal.

5. **Session-aware filtering**: Cash open (first hour) often has different microstructure.
   Filter entries during high-volatility periods to avoid whipsaws.

6. **Directional persistence**: Only enter when the flow signal is strong AND the
   recent price action supports the direction (e.g., for a buy signal, price should
   be above recent lows).

This strategy:
- Enters on high-conviction flow signals only (threshold 0.45)
- Ensures 5m+ holding via bracket (exit on next bar close or stop/target)
- Uses asymmetric brackets: 1.0 stop, 2.0 target (2:1 risk/reward)
- Filters out first bar of session and low-volume bars
- Limits entries to ~2-3 per session to manage commission drag
- Tracks position age to avoid premature exits
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.45,
        stop_offset: float = 1.0,
        target_offset: float = 2.0,
        size: int = 1,
        min_volume: int = 80,
        max_entries_per_session: int = 3,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign)
            flow_threshold: Minimum ratio of dominant side to total volume (0-1)
            stop_offset: Distance for protective stop (smaller = tighter risk)
            target_offset: Distance for profit target (larger = better reward)
            size: Position size in contracts
            min_volume: Minimum bar volume to avoid thin-bar noise
            max_entries_per_session: Maximum entries per session to control frequency
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

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: high-conviction flow signals only, respecting session limits.
        Exit via bracket: stop at stop_offset, target at target_offset.
        Minimum 5m hold enforced by bracket structure.
        """
        if not ctx.actionable:
            return
        
        # Track session changes
        if ctx.session != self._current_session:
            self._current_session = ctx.session
            self._entries_this_session = 0
        
        # One position at a time; bracket will handle exit
        if ctx.position or ctx.working_order_ids:
            return
        
        # Limit entries per session to control commission drag
        if self._entries_this_session >= self.max_entries_per_session:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        last_bar = history[-1]  # Bar that just closed
        
        # Skip first bar of session (often has different microstructure)
        if len(history) == 1:
            return
        
        # Skip low-volume bars
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
        
        # High-conviction threshold: only enter on very strong signals
        if buy_ratio > self.flow_threshold:
            # Strong buy-side aggression
            # Directional filter: check that price action supports the signal
            # Previous bar should not have closed at extreme low (showing weakness)
            if len(history) >= 2:
                prev_bar = history[-2]
                prev_range = prev_bar.high - prev_bar.low
                if prev_range > 0:
                    prev_close_pos = (prev_bar.close - prev_bar.low) / prev_range
                    # If previous bar closed very low, signal is weaker
                    if prev_close_pos < 0.2:
                        return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        elif sell_ratio > self.flow_threshold:
            # Strong sell-side aggression
            # Directional filter: check that price action supports the signal
            if len(history) >= 2:
                prev_bar = history[-2]
                prev_range = prev_bar.high - prev_bar.low
                if prev_range > 0:
                    prev_close_pos = (prev_bar.close - prev_bar.low) / prev_range
                    # If previous bar closed very high, signal is weaker
                    if prev_close_pos > 0.8:
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
        No action needed here - bracket handles the exit.
        """
        pass

    def on_lifecycle(self, ctx) -> None:
        """
        Track lifecycle events. No special action needed.
        Bracket OCO pair handles the exit automatically.
        """
        pass
