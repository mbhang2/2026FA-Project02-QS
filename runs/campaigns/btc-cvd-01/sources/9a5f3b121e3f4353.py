"""Cumulative volume delta (CVD) continuation on the BTC perpetual.

The claim under test
--------------------
When aggressive buying outweighs aggressive selling on the BTC perpetual, price
keeps rising for a while; when aggressive selling outweighs buying, price keeps
falling. This file tests that direction -- it does not fade flow.

How "large" is measured
-----------------------
Order flow is aggregated into fixed four-hour blocks (six per UTC day, aligned
to the epoch, so they never straddle a session boundary). For each block:

    imbalance = sum(buy_volume - sell_volume) / sum(volume)   in [-1, +1]

Dividing by the block's own volume is the first normalisation: BTC volume moves
by an order of magnitude across 2023-2026 and across the hours of a day, and a
ratio is comparable where a raw contract count is not.

The second normalisation is against recent flow: the last 90 completed blocks
(15 days) are kept on ``self`` and the current block's imbalance is converted to
a z-score against them. So "large" means large *relative to what this market has
recently been doing*, not large relative to a constant chosen by me. The
reference blocks are non-overlapping, which keeps the dispersion estimate honest
-- overlapping windows would understate the standard deviation and fire far more
often than the threshold implies.

Entry, horizon and exit
-----------------------
At a block boundary, z >= +2.5 goes long and z <= -2.5 goes short. The horizon is
fixed at one block (four hours), matching the measurement window: the claim is
about the next comparable window, so the exit is a bar clock, not a second,
untuned signal. That also keeps turnover low, which matters more here than
anywhere: commission is 5 bp per side, so a round trip costs 10 bp of notional
before spread, and the four-hour hold is what the edge has to clear.

Entry is a market order. The signal is continuation -- being late by 100 ms of
latency is the cost of being in at all, and a resting limit that only fills when
price comes back to it is a different (adverse-selection) bet.

Protection is a real stop order armed at the fill, not a check on the next bar,
so a four-hour hold is never naked between callbacks. It is deliberately wide
(6x the average 5-minute range over the last 96 bars, floored at 1.5% of price):
a disaster stop, not an exit. A tight stop would fire on the noise inside the
measured horizon and convert the signal into a series of small losses plus two
round trips.

What is deliberately not here
-----------------------------
No session-end flattening: crypto has no close, and the P&L is scored against
each session's opening price regardless of whether a position is carried. No
funding avoidance: at ~1 bp per settlement against a 10 bp round trip, funding
is second-order and avoiding it would distort the hold. No volume-weighted
average price, no regime filter, no multi-instrument input -- the campaign asks
for the simple version first. Ticks are not subscribed: ``bar.delta`` already
carries the aggressor split at bar resolution, and a tick subscription would
multiply the work for no extra information at a four-hour horizon.

Everything the strategy keeps across callbacks lives on ``self`` (the reference
distribution, the partial block accumulators, the bar count of the open trade);
nothing outside the current bar is read from ``ctx.history``, and the depth read
there (96 bars) is far inside the 512-bar window.
"""

from __future__ import annotations

from collections import deque

_NS_PER_HOUR = 3_600 * 1_000_000_000


class Strategy:
    """Four-hour cumulative-delta imbalance, held for four hours."""

    ENTRY_TAG = "cvd-entry"
    STOP_TAG = "cvd-stop"
    BLOCK_NS = 4 * _NS_PER_HOUR

    def __init__(
        self,
        *,
        interval: str = "5m",
        hold_bars: int = 48,
        ref_len: int = 90,
        min_ref: int = 30,
        entry_z: float = 2.5,
        atr_bars: int = 96,
        stop_mult: float = 6.0,
        min_stop_frac: float = 0.015,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.hold_bars = hold_bars          # 48 x 5m = one four-hour block
        self.ref_len = ref_len              # blocks of reference flow kept
        self.min_ref = min_ref              # blocks needed before trading
        self.entry_z = entry_z
        self.atr_bars = atr_bars
        self.stop_mult = stop_mult
        self.min_stop_frac = min_stop_frac
        self.size = size

        self.imbs: deque[float] = deque(maxlen=ref_len)
        self._block_id: int | None = None
        self._blk_delta: int = 0
        self._blk_vol: int = 0

        self._stop_dist: float = 0.0
        self.bars_held: int = 0
        self.exiting: bool = False
        self._exit_wait: int = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        # Declared once. Only one subscription: bar.delta already carries the
        # aggressor split, so ticks would cost 5x for no extra information at
        # this horizon.
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        history = ctx.history(self.interval)
        if not history:
            return
        bar = history[-1]

        # Advance the four-hour flow block. Returns a z-score only on the first
        # bar of a new block, i.e. six opportunities a day.
        z = self._roll(bar)

        if not ctx.actionable:
            return

        if ctx.position != 0:
            self._manage(ctx)
            return

        # Flat.
        self.bars_held = 0
        self.exiting = False
        self._exit_wait = 0

        # A protective stop left over from a finished trade would block every
        # future entry, because it counts as a working order. Clear it first.
        stale = ctx.find_orders(tag=self.STOP_TAG)
        if stale:
            for order in stale:
                ctx.cancel(order.id)
            return

        if z is None or ctx.working_order_ids:
            return

        if z >= self.entry_z:
            if ctx.buy(size=self.size, tag=self.ENTRY_TAG) is not None:
                self._stop_dist = self._stop_distance(history, bar.close)
                self.bars_held = 0
        elif z <= -self.entry_z:
            if ctx.sell(size=self.size, tag=self.ENTRY_TAG) is not None:
                self._stop_dist = self._stop_distance(history, bar.close)
                self.bars_held = 0

    # -------------------------------------------------------------- position

    def _manage(self, ctx) -> None:
        if self.exiting:
            # A queued market close resolves within one 100 ms hop; if the book
            # somehow did not take it, retry rather than sit here forever.
            self._exit_wait += 1
            if self._exit_wait < 6:
                return
            self.exiting = False
            self._exit_wait = 0

        self.bars_held += 1
        if self.bars_held < self.hold_bars:
            return
        self._flatten(ctx)

    def _flatten(self, ctx) -> None:
        # Cancel the protective stop before flattening: a reduce-only stop left
        # resting with no position behind it is noise in the blotter.
        for order in ctx.find_orders(tag=self.STOP_TAG):
            ctx.cancel(order.id)
        if ctx.position != 0:
            try:
                ctx.close()
            except Exception:      # already flat by the time the queue applies
                pass
        self.exiting = True
        self._exit_wait = 0

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx) -> None:
        fill = ctx.fill

        if fill.position == 0:
            # Flat: the trade is over, whichever leg ended it.
            for order in ctx.find_orders(tag=self.STOP_TAG):
                ctx.cancel(order.id)
            self.bars_held = 0
            self.exiting = False
            self._exit_wait = 0
            return

        if ctx.find_orders(tag=self.STOP_TAG):
            return

        dist = self._stop_dist
        if dist <= 0:
            dist = self.min_stop_frac * fill.price
        qty = abs(int(fill.position))
        if qty <= 0:
            return

        # First instant protection can exist: armed here, not on the next bar.
        if fill.position > 0:
            ctx.sell(
                size=qty,
                type="stop",
                trigger=fill.price - dist,
                tif="GTC",
                reduce_only=True,
                tag=self.STOP_TAG,
            )
        else:
            ctx.buy(
                size=qty,
                type="stop",
                trigger=fill.price + dist,
                tif="GTC",
                reduce_only=True,
                tag=self.STOP_TAG,
            )

    def on_lifecycle(self, ctx) -> None:
        # No order id is held across callbacks; every order is found by tag, so
        # a DAY/GTD expiry cannot leave a stale id here.
        pass

    # --------------------------------------------------------------- helpers

    def _roll(self, bar) -> float | None:
        """Accumulate the current four-hour block; score the one just finished."""
        block_id = bar.open_ts // self.BLOCK_NS
        z = None

        if self._block_id is None:
            self._block_id = block_id
        elif block_id != self._block_id:
            if self._blk_vol > 0:
                z = self._push(self._blk_delta / self._blk_vol)
            self._block_id = block_id
            self._blk_delta = 0
            self._blk_vol = 0

        self._blk_delta += bar.delta
        self._blk_vol += bar.volume
        return z

    def _push(self, imb: float) -> float | None:
        """z-score of a new block imbalance against the trailing distribution."""
        z = None
        n = len(self.imbs)
        if n >= self.min_ref:
            values = list(self.imbs)
            mean = sum(values) / n
            var = sum((v - mean) * (v - mean) for v in values) / n
            sd = var ** 0.5
            if sd > 1e-9:
                z = (imb - mean) / sd
        self.imbs.append(imb)
        return z

    def _stop_distance(self, history, price: float) -> float:
        """Wide disaster stop: 6x the average 5m range, floored at 1.5%."""
        window = history.last(self.atr_bars)
        floor = self.min_stop_frac * price
        if not window:
            return floor
        span = 0.0
        for b in window:
            span += b.high - b.low
        avg = span / len(window)
        return max(self.stop_mult * avg, floor)
