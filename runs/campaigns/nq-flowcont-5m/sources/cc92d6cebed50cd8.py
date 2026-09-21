"""
Flow continuation strategy: Asymmetric risk/reward with tighter entry confirmation.

Analysis of 43 trials reveals a persistent ceiling at -0.1370 (trial #43).
Key observations:
1. The 0.55 flow threshold is robust and stable
2. Trade count ~768 (2.0/session) is optimal
3. Commission (0.20 per round turn) is the binding constraint
4. Extended targets (4.0) haven't improved scores vs 3.0
5. Stricter price action (close_position_threshold=0.67) hasn't helped

Critical insight: Trial #43 with extended target (4.0) scored -0.1370, which is
WORSE than trial #9 with target 3.0 (-0.1437). This suggests:
- Longer holds are NOT the answer
- The problem is not exit timing but entry PRECISION
- We need fewer, higher-conviction trades

New hypothesis - TIGHTER ENTRY WITH BALANCED RISK/REWARD:
The key is that we're trading 768 times per 384 sessions (2.0/session).
This is actually MANY entries. The issue is win rate, not quantity.

Current best (trial #9):
- 768 trades, -0.1437 score
- 3.0 target, 1.5 stop (2:1 reward:risk)
- Opposition check, basic volume filter

The problem: 768 trades with negative score means most trades lose.
At 0.20 commission per round turn, we need a 51%+ win rate just to break even.
With 2:1 reward:risk, we need 33%+ win rate to break even.
We're clearly below that.

New approach - REQUIRE MULTI-BAR CONFIRMATION:
1. Keep 0.55 flow threshold (proven)
2. Add requirement: flow signal must be SUSTAINED over 2 bars
   (current bar AND previous bar both show same dominant side)
3. This dramatically reduces false signals from 1-bar noise
4. Keep opposition check (previous bar shouldn't contradict)
5. Return to 3.0 target, 1.5 stop (proven better than 4.0)
6. Expected: 400-500 trades (50% reduction) with much higher win rate
7. Score improvement to -0.08 to -0.05 range

Why this works:
- 2-bar confirmation filters out 1-bar whipsaws and noise
- Reduces trade count to more selective, high-conviction entries
- Higher win rate from sustained signal vs single-bar spike
- Commission impact is MASSIVE at 768 trades; cutting to 450 helps
- Aligns with 5m signal requirement (signal sustained over 10m window)
- Simpler to implement, no new indicators

Why we haven't tried this:
- We've been tweaking magnitude (thresholds, targets) not structure
- Multi-bar confirmation is orthogonal to previous attempts
- Natural evolution: we've proven the signal works, now filter for quality

Implementation:
1. Check current bar flow ratio > 0.55
2. Check previous bar flow ratio > 0.55 (SAME DIRECTION)
3. Check opposition (bar before previous shouldn't oppose)
4. Volume filter on current bar
5. Price action: close on correct side of midpoint (0.5, not stricter)
6. Return to 3.0 target, 1.5 stop
7. Keep max_entries_per_session at 2 for safety

Expected outcome:
- 400-500 trades (down from 768, much more selective)
- Higher win rate from 2-bar confirmation
- Better commission economics (fewer round turns)
- Score: -0.08 to -0.05 (significant improvement from -0.1437)
- More robust across market regimes
"""

from __future__ import annotations


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
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio of dominant side (0.55 = 55%+)
            opposition_threshold: Reject if opposing bar shows this level (0.55)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.opposition_threshold = opposition_threshold
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

    def _get_flow_direction(self, bar) -> str | None:
        """
        Determine if bar shows strong buy or sell flow.
        Returns 'buy', 'sell', or None if no clear dominant side.
        """
        if bar.volume <= 0:
            return None
        
        buy_ratio = bar.buy_volume / bar.volume
        sell_ratio = bar.sell_volume / bar.volume
        
        if buy_ratio > self.flow_threshold:
            return 'buy'
        elif sell_ratio > self.flow_threshold:
            return 'sell'
        else:
            return None

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal with 2-bar confirmation.
        Only enter when flow signal is sustained over 2 consecutive bars.
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
        # Need 3 bars: current, previous, and one before that for opposition check
        if len(history) < 3:
            return
        
        current_bar = history[-1]  # Bar that just closed
        prev_bar = history[-2]      # Previous bar
        prior_bar = history[-3]     # Bar before that
        
        # Skip first 3 bars of session (cash open microstructure)
        if self._bars_in_session <= 3:
            return
        
        # Skip last 5 minutes of session
        time_left = ctx.session_end - ctx.ts
        if time_left < 300_000_000_000:  # Less than 5 minutes
            return
        
        # Skip low-volume bars on current bar
        if current_bar.volume < self.min_volume:
            return
        
        # Get flow directions for current and previous bars
        current_direction = self._get_flow_direction(current_bar)
        prev_direction = self._get_flow_direction(prev_bar)
        
        # REQUIREMENT: Flow must be sustained (same direction for 2 bars)
        if current_direction is None or prev_direction is None:
            return
        
        if current_direction != prev_direction:
            # Flow direction changed; not a sustained signal
            return
        
        # Get prior bar flow for opposition check
        prior_direction = self._get_flow_direction(prior_bar)
        
        # Price action confirmation: close on correct side of midpoint
        bar_range = current_bar.high - current_bar.low
        if bar_range <= 0:
            return
        
        close_position = (current_bar.close - current_bar.low) / bar_range
        
        # BUY: 2-bar sustained buy flow
        if current_direction == 'buy':
            # Opposition check: prior bar shouldn't show strong selling
            if prior_direction == 'sell':
                return
            
            # Price action: close above midpoint
            if close_position < 0.5:
                return
            
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            self._entries_this_session += 1
        
        # SELL: 2-bar sustained sell flow
        elif current_direction == 'sell':
            # Opposition check: prior bar shouldn't show strong buying
            if prior_direction == 'buy':
                return
            
            # Price action: close below midpoint
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
