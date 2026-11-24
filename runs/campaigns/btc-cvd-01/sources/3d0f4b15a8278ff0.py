"""CVD continuation on the BTC perpetual, tested against its own costs.

The campaign's claim: unusually large positive cumulative volume delta is
followed by higher prices, and unusually large negative CVD by lower prices,
over the next comparable window. This file is a direct test of that, not a fade.

MEASUREMENT
-----------
`bar.delta` is `buy_volume - sell_volume`, so cumulative volume delta over a
window is just the sum of `bar.delta` over that window. "Large" is made
scale-free two ways at once, because BTC volume and volatility differ by an
order of magnitude across hours and years and a fixed contract count means
different things in each:

  1. Normalise by recent *flow*: imbalance = sum(delta) / sum(volume) over the
     window, which is a fraction of aggressive flow rather than a size.
  2. Normalise by recent *behaviour*: turn that fraction into a z-score against
     the distribution of the same statistic over the last `z_window` bars, so a
     quiet regime and a violent one are judged on their own scales.

A small absolute floor on the imbalance is also required. Without it the
z-score will happily fire in a regime where the whole imbalance distribution
has collapsed, calling a 0.3% flow skew "extreme" -- the classic way a
self-normalising signal trades pure noise.

HORIZON
-------
15-minute bars; the flow window is 8 bars (two hours) and the time exit is 8
bars. Signal horizon and holding period are the same length, which is the
honest form of "the next comparable window" -- the claim does not say the
effect lasts forever, so a strategy that measures over two hours and holds for
ten must state which one it is testing.

COSTS AND WHY IT TRADES RARELY
------------------------------
5 bp a side is 10 bp a round trip, about a seventh of a two-hour BTC move.
A flow signal that fires on every moderate imbalance would pay that several
times a day and be right for nothing. So entries are gated hard: z >= 2, a
real absolute imbalance, and a mandatory `max_hold + cooldown` gap between
entries. Daily round trips are counted in single digits.

Orders are **market** orders. The book-queue-pessimistic fill model fills a
resting limit only when trades go strictly through it, and for a momentum
signal that is pure adverse selection -- a bid under the market in a rising
tape fills exactly when the tape has stopped rising. Paying the touch is the
correct cost for this claim; the resting-limit version of the same claim is a
different experiment and would need its own argument.

RISK
----
A protective bracket is placed with the entry, in **offsets** rather than
prices, so a fill that slips cannot invert a leg and crash the run, and the
engine arms it at the instant of the fill rather than on the next callback.
The stop is 3 ATR (with a 15 bp floor) and the target 8 ATR (with a 40 bp
floor), so in practice almost every trade leaves on the time exit and the stop
is a disaster stop, not a strategy component.

STATE
-----
Everything is maintained incrementally on `self` as bars arrive. Nothing walks
`ctx.history` more than once per bar, and nothing reaches past the pushed
window.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Follow unusually large aggressive flow, normalised two ways."""

    def __init__(
        self,
        interval: str = "15m",
        flow_window: int = 8,
        z_window: int = 192,
        z_entry: float = 2.0,
        min_abs_imbalance: float = 0.03,
        min_samples: int = 64,
        max_hold: int = 8,
        cooldown: int = 8,
        atr_window: int = 32,
        stop_atr: float = 3.0,
        target_atr: float = 8.0,
        stop_floor: float = 0.0015,
        target_floor: float = 0.0040,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.flow_window = flow_window
        self.z_window = z_window
        self.z_entry = z_entry
        self.min_abs_imbalance = min_abs_imbalance
        self.min_samples = min_samples
        self.max_hold = max_hold
        self.cooldown = cooldown
        self.atr_window = atr_window
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.stop_floor = stop_floor
        self.target_floor = target_floor
        self.size = size

        # Incremental feature state. Bounded by construction.
        self.deltas: deque[float] = deque(maxlen=flow_window)
        self.volumes: deque[float] = deque(maxlen=flow_window)
        self.imb_hist: deque[float] = deque(maxlen=z_window)
        self.trs: deque[float] = deque(maxlen=atr_window)
        self.prev_close: float | None = None

        # Position bookkeeping.
        self.bars_held = 0
        self.cool = 0
        self.exiting = False
        self.was_in = False

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        # One subscription, but ask anyway: it is the question a multi-timeframe
        # strategy has, and it is free here.
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # --- incremental state, updated on every bar, warmup included --------
        if self.prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self.prev_close),
                abs(bar.low - self.prev_close),
            )
        self.trs.append(tr)
        self.prev_close = bar.close

        self.deltas.append(float(bar.delta))
        self.volumes.append(float(bar.volume))

        if len(self.deltas) < self.flow_window:
            return
        total_volume = sum(self.volumes)
        if total_volume <= 0.0:
            return
        imbalance = sum(self.deltas) / total_volume

        # The baseline accumulates on every bar, held or flat, or the z-score
        # would be computed against a distribution with holes in it.
        self.imb_hist.append(imbalance)

        # --- an open position is managed before anything else ----------------
        if ctx.position:
            self.was_in = True
            if self.exiting:
                return                      # a flatten is already in flight
            self.bars_held += 1
            if self.bars_held >= self.max_hold:
                self._flatten(ctx)
            return

        # --- flat ------------------------------------------------------------
        self.bars_held = 0
        self.exiting = False
        if self.was_in:
            self.was_in = False
            self.cool = self.cooldown       # never re-enter on the same episode

        if self.cool > 0:
            self.cool -= 1
            return

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return                          # an entry is already in flight
        if len(self.imb_hist) < self.min_samples:
            return

        n = len(self.imb_hist)
        mean = sum(self.imb_hist) / n
        var = sum((x - mean) ** 2 for x in self.imb_hist) / n
        if var <= 0.0:
            return
        sd = var ** 0.5
        z = (imbalance - mean) / sd

        long_signal = z >= self.z_entry and imbalance >= self.min_abs_imbalance
        short_signal = z <= -self.z_entry and imbalance <= -self.min_abs_imbalance
        if not long_signal and not short_signal:
            return

        if not self.trs:
            return
        atr = sum(self.trs) / len(self.trs)
        if atr <= 0.0:
            return

        # Offsets, not prices: measured from the actual fill, so a slipped
        # market order cannot put a leg on the wrong side of the entry.
        stop_offset = max(self.stop_atr * atr, bar.close * self.stop_floor)
        target_offset = max(self.target_atr * atr, bar.close * self.target_floor)

        if long_signal:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="cvd-long",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="cvd-short",
            )
        if order_id is None:
            return                          # action cap dropped it; no order

        self.bars_held = 0
        self.exiting = False

    # --------------------------------------------------------------- helpers

    def _flatten(self, ctx) -> None:
        """Pull the bracket legs, then close at market. Never flips."""
        for order in ctx.working_orders:
            ctx.cancel(order.id)
        ctx.close()
        self.exiting = True

    # ------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        # Position bookkeeping is done from `ctx.position` on the next bar; the
        # bracket is armed by the engine at the instant of the fill, so there is
        # nothing to do here that would not be a race with it.
        pass

    def on_lifecycle(self, ctx) -> None:
        # No order id is ever kept on `self`, so a DAY expiry at a session close
        # cannot leave this strategy holding a stale one.
        pass
