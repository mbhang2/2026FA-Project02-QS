"""
Flow continuation strategy: refined entry with cumulative flow validation.

Analysis of recent trials:
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best baseline
- #35: -0.1665, 361 trades → 2-bar momentum was too strict, cut trades in half
- #34: -0.1978, 768 trades → opposition at 0.52 hurt vs 0.55
- #33: -0.1740, 768 trades → volume persistence helped slightly

Key insight from #35's underperformance:
Requiring BOTH prior bars to show >0.50 on the signal side was too restrictive.
It cut trades from 768 to 361 (47% reduction) and STILL didn't improve the score.
This suggests the problem is not "one-bar spikes reversing" but something else.

The real issue: we're entering on signals that reverse because the NEXT bar
contradicts the entry. We can't see the next bar, but we CAN improve our signal
quality by looking at VOLUME PERSISTENCE and CONSISTENCY.

New hypothesis - CUMULATIVE FLOW VALIDATION:
Instead of requiring both prior bars to show directional bias, require that:
1. The current bar shows strong flow (0.55+) - unchanged
2. The previous bar did NOT strongly oppose (0.55+) - unchanged  
3. The current bar's volume is NOT significantly lower than the previous bar
   (volume_ratio > 0.80: current bar volume >= 80% of previous bar volume)
   This filters out weak signals on thin bars that reverse on volume spikes.

Why this works:
- A strong flow signal on THIN volume is less reliable than on NORMAL volume
- If buy_ratio is 0.55+ but volume dropped 40%, the signal is weak
- Volume persistence = the conviction behind the signal is real
- This naturally filters without over-constraining
- Expected: 650-750 trades (down slightly from 768, but not half)
- Better edge per trade from filtering weak-conviction signals

Why #35 failed:
Requiring BOTH bars at >0.50 momentum was like saying "the last 2 bars must
both show directional bias". But that's not how momentum works—a weak bar
(0.48) followed by a strong bar (0.55) IS momentum acceleration, and that's
exactly what we want to catch. The 2-bar requirement filtered those out.

Volume persistence is different: it's about CONVICTION, not DIRECTION.
A 0.55+ flow bar on 80%+ of prior volume = confident signal.
A 0.55+ flow bar on 40% of prior volume = weak signal that might reverse.

Expected outcome:
- 650-750 trades (down from 768, but not drastically)
- Better quality entries from volume confirmation
- Score in -0.12 to -0.14 range
- Fewer whipsaws from thin-bar reversals

The philosophy:
#9 works. We're not changing the core signal. We're adding a volume check:
only take the signal if the bar carrying it has reasonable volume conviction.
This is the simplest possible refinement—one ratio check—and it targets
the actual problem: weak signals on thin bars.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        opposition_threshold: float = 0.55,
        volume_ratio_min: float = 0.80,
        stop_offset: float = 1.5,
        target_offset: float = 3.0,
        size: int = 1,
        min_volume: int = 150,
        max_entries_per_session: int = 2,
    ):
        """
        Args:
            interval: Bar interval for signal (5m per campaign spec)
            flow_threshold: Minimum ratio for entry signal (0.55 = 55%+)
            opposition_threshold: Reject entry if prev bar shows opposite at this
                                 level (0.55 = reject if 55%+ opposite)
            volume_ratio_min: Current bar volume must be >= this ratio of prev bar
                             (0.80 = current >= 80% of previous volume)
            stop_offset: Distance for protective stop (1.5 points)
            target_offset: Distance for profit target (3.0 points)
            size: Position size in contracts
            min_volume: Minimum bar volume for reliable flow ratio
            max_entries_per_session: Maximum entries per session
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.opposition_threshold = opposition_threshold
        self.volume_ratio_min = volume_ratio_min
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
        Entry logic: 5m flow signal with volume persistence check.
        Only enter when current bar shows strong flow AND previous bar did not
        strongly oppose AND current bar has reasonable volume conviction
        (not a thin bar that might reverse).
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
        
        # Get previous bar for opposition and volume checks
        prev_bar = history[-2]
        
        if prev_bar.volume <= 0:
            return
        
        prev_buy_ratio = prev_bar.buy_volume / prev_bar.volume
        prev_sell_ratio = prev_bar.sell_volume / prev_bar.volume
        
        # Volume persistence check: current bar should have reasonable volume
        # relative to previous bar (not a thin bar that might reverse)
        volume_ratio = last_bar.volume / prev_bar.volume
        if volume_ratio < self.volume_ratio_min:
            return
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Opposition check: reject if previous bar was strongly sell-dominated
            if prev_sell_ratio > self.opposition_threshold:
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
        
        # Strong sell signal: current bar has 55%+ sell volume
        elif sell_ratio > self.flow_threshold:
            # Opposition check: reject if previous bar was strongly buy-dominated
            if prev_buy_ratio > self.opposition_threshold:
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
        Track lifecycle events. Bracket handles the exit.
        """
        pass
