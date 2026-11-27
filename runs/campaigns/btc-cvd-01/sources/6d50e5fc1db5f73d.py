"""Cumulative volume delta continuation on the OKX BTC perpetual.

The claim under test: unusually one-sided aggressive flow is followed by
continuation in the same direction. This file tests exactly that direction --
it never fades -- and nothing else.

Signal
------
`bar.delta` is `buy_volume - sell_volume` on the bar, so cumulative volume
delta over a window is just the sum of `delta`. "Large" is made scale-free by
dividing by the total volume in the same window:

    imbalance = sum(delta) / sum(volume)   over the last `lookback` bars

That is a number in roughly [-1, 1] whose meaning does not drift when BTC
volume triples or collapses, which a fixed contract threshold would. It needs
no fitting, so this is an ordinary (not a fitted) proposal.

Costs drive every other choice. A round trip costs 10bp of notional before
spread, so the horizon has to be long enough that the move being predicted is
several times the fee. Hence 1h bars, a 12-hour flow window and a 12-hour
hold: roughly a 0.1-0.2% move is needed to break even, and a 12-hour BTC move
is comfortably larger than that when the signal is right. Trading every bar on
a 5m chart would pay the fee hundreds of times and could not win.

Turnover control
----------------
Two mechanisms, because a persistent imbalance would otherwise re-enter the
same trend every time the previous hold expires:

  * after any exit the signal must *reset* -- |imbalance| has to fall back
    below `reset_frac * threshold` -- before the same side can be traded
    again. A flow that is still extreme when the hold ends is the same trade,
    not a new one.
  * a short cooldown after every exit.

Entry / exit
------------
Market entries: the signal is a statement about what already happened, and a
resting limit would too often not be there when the continuation starts.
Protection is a bracket in *offsets*, which the engine arms at the instant of
the fill -- an absolute level derived from the pre-fill price inverts as soon
as the market order slips a tick. The stop is wide (2.5%, well outside 12h
noise) and exists for tail protection only; the real exit is the clock. A
distant take-profit is attached so the bracket is symmetric and a runaway move
is banked rather than ridden back.

Holding through a funding stamp (00:00, 08:00, 16:00 UTC) happens sometimes;
that is a cost of a 12-hour hold and is accepted, not engineered around.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade with unusually one-sided aggressive flow; hold for a fixed horizon."""

    def __init__(
        self,
        *,
        interval: str = "1h",
        lookback: int = 12,
        threshold: float = 0.30,
        reset_frac: float = 0.5,
        hold_bars: int = 12,
        cooldown_bars: int = 4,
        size: int = 100,
        stop_pct: float = 0.025,
        target_pct: float = 0.06,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.threshold = threshold
        self.reset_frac = reset_frac
        self.hold_bars = hold_bars
        self.cooldown_bars = cooldown_bars
        self.size = size
        self.stop_pct = stop_pct
        self.target_pct = target_pct

        # Rolling (delta, volume) for the last `lookback` closed bars. Kept on
        # self because it is small, exact, and cheaper than re-reading a window.
        self.flow: deque = deque(maxlen=lookback)

        self.bars_held = 0
        self.armed = True
        self.was_in = False
        self.exiting = False
        self.cooldown = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------ helpers

    def _imbalance(self):
        """Signed share of volume over the window, or None if not yet defined."""
        if len(self.flow) < self.lookback:
            return None
        total = 0
        signed = 0
        for delta, volume in self.flow:
            total += volume
            signed += delta
        if total <= 0:
            return None
        return signed / total

    def _start_exit(self, ctx) -> None:
        self.exiting = True
        # Pull the bracket so it cannot fire after the position is gone, then
        # flatten. Both are queued; the cancel is applied first.
        ctx.cancel_all()
        ctx.close()

    # ------------------------------------------------------------------ callbacks

    def on_bars(self, ctx) -> None:
        bar = ctx.bars.get(self.interval)
        if bar is None:
            return
        self.flow.append((bar.delta, bar.volume))

        if not ctx.actionable:
            return

        # ---- in a position: the clock is the exit -------------------------
        if ctx.position != 0:
            self.was_in = True
            if self.exiting:
                # Previous flatten has not landed yet; keep asking.
                ctx.cancel_all()
                ctx.close()
                return
            self.bars_held += 1
            if self.bars_held >= self.hold_bars:
                self._start_exit(ctx)
            return

        # ---- flat ----------------------------------------------------------
        if self.exiting:
            self.exiting = False
        if self.was_in:
            # Left by the protective stop or the take-profit, not by the clock.
            self.was_in = False
            self.bars_held = 0
            self.armed = False
            self.cooldown = self.cooldown_bars

        if ctx.working_order_ids:
            return  # an entry is already in flight

        if self.cooldown > 0:
            self.cooldown -= 1
            return

        imbalance = self._imbalance()
        if imbalance is None:
            return

        # Re-arm only once the old flow has decayed: a still-extreme reading is
        # the trade we just took, not a new one.
        if not self.armed:
            if abs(imbalance) < self.threshold * self.reset_frac:
                self.armed = True
            return

        if abs(imbalance) < self.threshold:
            return

        price = ctx.last_price
        if not price or price <= 0:
            return

        stop = price * self.stop_pct
        target = price * self.target_pct

        if imbalance > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="cvd-long",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="cvd-short",
            )

        if order_id is None:
            return  # dropped by the action cap; try again next bar

        self.bars_held = 0
        self.armed = False

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
