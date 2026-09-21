"""
Flow continuation strategy: adaptive threshold with session-level filtering.

Analysis of recent trials:
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best baseline
- #30: -0.1913, 768 trades (2.0/session) → momentum persistence hurt
- #29: -0.3693, 768 trades (2.0/session) → 1m confirmation underperformed
- #19: -0.1631, 382 trades (0.99/session) → fewer trades competitive

Core insight from best performers:
The simplest approaches work best. #9's -0.1437 uses:
- 0.55 flow threshold (solid)
- Previous bar opposition check (effective)
- Simple price action (close above/below midpoint)
- Session discipline (skip first 3, last 5 min)
- Volume filter (min 150)

The remaining problem:
We're trading 2.0 times per session but still negative. Commission is 0.20 per
round turn. At 768 trades across 384 sessions, we're paying ~0.10 per trade in
commission. Our edge per trade must be >0.10 just to break even.

New hypothesis - ADAPTIVE SELECTIVITY:
Instead of trading every signal that passes the filter, track which SESSIONS
have been profitable so far (or at least not terrible). Then be MORE selective
in bad sessions and LESS selective in good ones.

Implementation:
1. Keep the core #9 logic (0.55 threshold, prev bar check, price action)
2. Add session-level tracking: track realized P&L per session
3. In sessions that are running negative, raise the bar slightly:
   - Increase flow threshold to 0.56 (require stronger signal)
   - Require BOTH prev bars to show agreement, not just avoid opposition
4. In sessions running positive or flat, use standard #9 rules
5. This is a meta-filter: same underlying signal, but adapted to session context

Why this works:
- We keep the signal that works (#9's core)
- We reduce exposure in sessions that aren't working
- This is NOT curve-fitting per session (we don't know future)
- This is risk management: tighten stops when losing, loosen when winning
- Expected: fewer total trades, but better quality in bad sessions

Expected outcome:
- 650-750 trades (10-15% fewer, from selectivity in bad sessions)
- Better P&L from avoiding bad session trades
- Score in -0.10 to -0.13 range (improvement from #9's -0.1437)

The philosophy:
#9 is the baseline. The signal works. The problem is we're mechanical about it.
By tracking session performance and tightening entry criteria in losing sessions,
we reduce the damage without killing the signal. This is adaptive, not fitted.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold_base: float = 0.55,
        flow_threshold_tight: float = 0.56,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold_base: Base flow threshold (0.55 = 55%+)
            flow_threshold_tight: Tighter threshold in losing sessions (0.56 = 56%+)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold_base = flow_threshold_base
        self.flow_threshold_tight = flow_threshold_tight
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        self.min_volume = min_volume
        self.max_entries_per_session = max_entries_per_session
        
        # Track entries per session
        self._current_session: int | None = None
        self._entries_this_session: int = 0
        self._bars_in_session: int = 0
        
        # Track session P&L for adaptive selectivity
        self._session_pnl: dict[int, float] = {}

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars."""
        ctx.subscribe(self.interval)

    def _get_flow_threshold(self) -> float:
        """
        Determine the flow threshold based on current session's P&L.
        If the session is running negative, use a tighter threshold.
        Otherwise use the base threshold.
        """
        if self._current_session is None:
            return self.flow_threshold_base
        
        session_pnl = self._session_pnl.get(self._current_session, 0.0)
        
        # If session is running negative, tighten the threshold
        if session_pnl < 0:
            return self.flow_threshold_tight
        
        return self.flow_threshold_base

    def _check_prev_bar_agreement(self, prev_bar_1, prev_bar_2, direction: str) -> bool:
        """
        Check if previous bars show agreement with the signal direction.
        direction: "buy" or "sell"
        
        In losing sessions, require BOTH previous bars to show some agreement.
        In neutral/winning sessions, require just that prev_bar_1 doesn't oppose.
        """
        if prev_bar_1.volume <= 0 or prev_bar_2.volume <= 0:
            return False
        
        prev_buy_ratio_1 = prev_bar_1.buy_volume / prev_bar_1.volume
        prev_sell_ratio_1 = prev_bar_1.sell_volume / prev_bar_1.volume
        prev_buy_ratio_2 = prev_bar_2.buy_volume / prev_bar_2.volume
        prev_sell_ratio_2 = prev_bar_2.sell_volume / prev_bar_2.volume
        
        if self._current_session is None:
            session_pnl = 0.0
        else:
            session_pnl = self._session_pnl.get(self._current_session, 0.0)
        
        if direction == "buy":
            if session_pnl < 0:
                # In losing sessions: require both bars show some buying
                return (prev_buy_ratio_1 > 0.50 and prev_buy_ratio_2 > 0.50)
            else:
                # In neutral/winning sessions: just require prev_1 doesn't oppose
                return prev_sell_ratio_1 <= self.flow_threshold_base
        else:  # sell
            if session_pnl < 0:
                # In losing sessions: require both bars show some selling
                return (prev_sell_ratio_1 > 0.50 and prev_sell_ratio_2 > 0.50)
            else:
                # In neutral/winning sessions: just require prev_1 doesn't oppose
                return prev_buy_ratio_1 <= self.flow_threshold_base

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal with adaptive threshold based on session P&L.
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
        if len(history) < 3:
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
        
        # Get previous two bars for agreement check
        prev_bar_1 = history[-2]
        prev_bar_2 = history[-3]
        
        # Get adaptive threshold based on session P&L
        flow_threshold = self._get_flow_threshold()
        
        # Strong buy signal: current bar has flow_threshold%+ buy volume
        if buy_ratio > flow_threshold:
            # Previous bar agreement check (adapts based on session P&L)
            if not self._check_prev_bar_agreement(prev_bar_1, prev_bar_2, "buy"):
                return
            
            # Price action confirmation: close above midpoint
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
                if close_position < 0.5:
                    return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # Strong sell signal: current bar has flow_threshold%+ sell volume
        elif sell_ratio > flow_threshold:
            # Previous bar agreement check (adapts based on session P&L)
            if not self._check_prev_bar_agreement(prev_bar_1, prev_bar_2, "sell"):
                return
            
            # Price action confirmation: close below midpoint
            bar_range = last_bar.high - last_bar.low
            if bar_range > 0:
                close_position = (last_bar.close - last_bar.low) / bar_range
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
        Track session P&L for adaptive selectivity.
        """
        # Update session P&L tracking
        if self._current_session is not None:
            self._session_pnl[self._current_session] = ctx.realized_pnl
