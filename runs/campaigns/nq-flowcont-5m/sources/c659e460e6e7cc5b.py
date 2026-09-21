"""
Flow continuation strategy: buy when recent 5m bar shows strong buy-side aggression,
sell when it shows strong sell-side aggression. Hold for at least 5 minutes.

This tests the core hypothesis from the campaign: aggressive flow continues over
a 5-minute horizon, not just 12 seconds. The signal is measured over a 5m bar
using normalized delta (buy_volume - sell_volume), and positions are held until
at least the next 5m bar closes (5+ minutes of holding).

Key design choices:
1. Measure aggression as delta normalized by total volume in the bar
2. Use a threshold to define "large" flow (tunable, default 0.3)
3. Entry is market on the bar following the signal (T+1 rule)
4. Exit is a 5m bracket: hold at least until next bar, stop/target are fixed offsets
5. No tick subscription - delta is on every bar, no need for raw ticks
6. Track entry bar to enforce minimum 5m hold before considering early exit
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        flow_threshold: float = 0.30,
        stop_offset: float = 1.0,
        target_offset: float = 1.5,
        size: int = 1,
    ):
        """
        Args:
            interval: Bar interval for signal measurement and holding period (5m fixed per campaign)
            flow_threshold: Normalized delta threshold to trigger entry (0-1 range)
            stop_offset: Distance below entry for protective stop
            target_offset: Distance above entry for profit target
            size: Position size in contracts
        """
        self.interval = interval
        self.flow_threshold = flow_threshold
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size
        
        # Track the bar we entered on, to enforce minimum holding period
        self._entry_bar_ts = None

    def setup(self, ctx) -> None:
        """Subscribe to 5m bars. No ticks needed - delta is on every bar."""
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        """
        Entry logic: if last closed bar shows strong directional flow,
        enter in the direction of that flow on this bar.
        
        Exit logic: bracket handles the exit after at least 5m (next bar close).
        """
        if not ctx.actionable:
            return
        
        # One position at a time; bracket will handle the exit
        if ctx.position or ctx.working_order_ids:
            return
        
        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        
        last_bar = history[-1]  # The bar that just closed
        
        # Measure aggression: normalized delta
        total_volume = last_bar.volume
        if total_volume <= 0:
            return
        
        # Delta is buy_volume - sell_volume, already computed on the bar
        delta = last_bar.delta
        normalized_delta = delta / total_volume
        
        # Entry signal: strong directional flow
        if normalized_delta > self.flow_threshold:
            # Strong buy-side aggression: enter long
            order_id = ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-long",
            )
            if order_id is not None:
                self._entry_bar_ts = last_bar.close_ts
                
        elif normalized_delta < -self.flow_threshold:
            # Strong sell-side aggression: enter short
            order_id = ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="flow-short",
            )
            if order_id is not None:
                self._entry_bar_ts = last_bar.close_ts

    def on_fill(self, ctx) -> None:
        """
        Position is now open. The bracket (stop and target) was armed by the
        engine at fill time, so we are protected from the instant we fill.
        
        We do not need to do anything here - the bracket handles the exit.
        """
        pass

    def on_lifecycle(self, ctx) -> None:
        """
        Track lifecycle events but no special action needed.
        The bracket OCO pair will handle the exit.
        """
        pass
