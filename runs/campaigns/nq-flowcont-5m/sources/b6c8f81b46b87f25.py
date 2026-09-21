"""
Flow continuation strategy: refined entry with 2-bar momentum confirmation.

Analysis of recent trials:
- #9/#10/#15: -0.1437, 768 trades (2.0/session) → best baseline
- #32: -0.2350, 768 trades → momentum filter (prev_buy_ratio < 0.52) was too strict
- #33: -0.1740, 768 trades → volume persistence helped slightly

Core insight:
The best performers (#9, #10, #15) all score -0.1437 with:
- 0.55 flow threshold
- Previous bar opposition check (reject if prev was opposite at 0.55+)
- Simple price action (close above/below midpoint)
- Session discipline (skip first 3 bars, last 5 minutes)
- Volume filter (min 150)
- Max 2 entries per session
- Stop 1.5, Target 3.0

Why #32 underperformed:
The momentum_min threshold of 0.52 was TOO STRICT. It required the previous bar
to show 52%+ buying momentum to enter on a 55%+ buy bar. This filtered out many
valid continuations where a weak bar (say 0.48 buy) is followed by a strong bar
(0.55+ buy). That's exactly the momentum acceleration we want to catch.

The problem with #33:
Volume persistence (current > previous) helped slightly (-0.1740 vs -0.1437),
but it's an additional constraint that might be filtering good signals.

New hypothesis - SIMPLER MOMENTUM CONFIRMATION:
Instead of requiring prev_buy_ratio >= 0.52, just require that the previous bar
was NOT strongly opposed. The opposition check already does this:
- For buy signal: reject if prev_sell_ratio > 0.55
- This means we accept if prev_sell_ratio <= 0.55
- That includes prev_buy_ratio from 0.0 to 0.45 (neutral/weak buying)

But what if we make the opposition threshold TIGHTER?
- Current: reject buy if prev_sell_ratio > 0.55 (i.e., prev was 55%+ sell)
- New: reject buy if prev_sell_ratio > 0.52 (i.e., prev was 52%+ sell)

This is less about "momentum acceleration" and more about "directional alignment".
We're saying: don't enter on a buy signal if the previous bar was moderately
or strongly bearish. This is a softer filter than #32's momentum_min.

Expected behavior:
- Fewer false entries than #9 (tighter opposition check)
- More entries than #32 (opposition check only, not strict momentum requirement)
- Trades should be 700-750 (down slightly from 768)
- Better edge per trade from filtering weak opposition
- Score in -0.12 to -0.14 range

The philosophy:
#9 is excellent. We're not trying to outsmart it. We're just making the
opposition check slightly tighter (0.52 instead of 0.55) to filter entries
where the previous bar showed some moderate bearish pressure. This is a small
tweak that should reduce whipsaws without over-filtering.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.55,
        opposition_threshold: float = 0.52,
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
            opposition_threshold: Reject entry if prev bar shows opposite side
                                 at this level (0.52 = reject if 52%+ opposite)
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

    def on_bars(self, ctx) -> None:
        """
        Entry logic: 5m flow signal with tighter opposition filter.
        Only enter when current bar shows strong flow AND the previous bar
        was not moderately or strongly opposed (opposition_threshold = 0.52).
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
        
        # Strong buy signal: current bar has 55%+ buy volume
        if buy_ratio > self.flow_threshold:
            # Tighter opposition check: reject if previous bar was moderately or
            # strongly sell-dominated (52%+ sell)
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
            # Tighter opposition check: reject if previous bar was moderately or
            # strongly buy-dominated (52%+ buy)
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
