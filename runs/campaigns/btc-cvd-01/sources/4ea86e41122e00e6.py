"""Cumulative volume delta continuation on the OKX BTCUSDT perpetual.

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling is followed by higher
prices over a comparable window, and the mirror for selling. This strategy
tests that direction and only that direction: it buys when signed flow is
unusually positive and sells when it is unusually negative. It never fades.

How "large" is measured
-----------------------
Two normalisations, both of which the data has to supply, because a fixed
contract count means different things at 03:00 and at 15:00 and different
things in 2023 and in 2026.

  1. Volume normalisation. Over the flow window (16 x 15m bars = 4 hours) the
     signal is signed volume as a *share* of total volume:

         imb = sum(buy_volume - sell_volume) / sum(volume)   in [-1, +1]

     Each bar carries its own aggressor split (`bar.delta`, `bar.volume`), so
     no tick subscription is needed -- and ticks cost roughly five times as
     much wall clock for the same information at bar resolution.

  2. Regime normalisation. The same statistic is recomputed on every bar and
     kept in a rolling history of the last 960 observations (about ten days of
     15m bars). The entry test is a z-score of today's 4h imbalance against
     that recent distribution, so "large" means large *for this market right
     now*, not large against a constant.

An entry needs both: z >= 2.0 (unusual) and |imb| >= 0.15 (economically
meaningful, not a statistical artefact of a very quiet window).

The horizon
-----------
The claim names "the next comparable window", so the holding period is the
same length as the measurement window: 16 bars, four hours. The position is
flattened on a time exit, and also early if the flow flips to the opposite
side beyond the meaningful threshold -- at that point the reason the trade
exists has gone, and holding on is a different, unstated claim.

Why entries are rare on purpose
-------------------------------
A round trip costs 10 basis points of notional in commission before spread and
depth; that is $30 on $30k and $100 on $100k, per round trip. A flow signal
that fires a few times an hour cannot pay that back. So:

  - the entry threshold is a two-sigma event on a four-hour aggregate, which
    is uncommon by construction;
  - after any exit the strategy re-arms only when the signal relaxes below
    z = 1.0 *or* flips sign. A persistent regime therefore produces one trade,
    not one trade per bar;
  - the write-off of an entire week with no trade is preferred to a week of
    paying 10bp to express an opinion the data did not support.

That is the honest test of the claim: if aggressive flow predicts the next few
hours, the few times it is unambiguous should pay for the many times it is not.

Execution
---------
Market entries. The fill model is book-queue-pessimistic: a resting limit
joins behind the displayed size and fills only when trades reach through it.
For a continuation entry, failing to be filled is a worse outcome than paying
the half-spread, and the commission dominates anyway. Protection is a bracket
given as *offsets*, so it is armed by the engine at the instant the fill lands
and cannot invert through a slip: a 0.8% stop and a wide 2.5% target that only
takes an unusually fast move.

No position is opened when less than one holding period remains before the
session boundary, because 00:00 UTC is a funding settlement and carrying into
it is a cost the signal did not ask for. The session end is published in
advance, so this is a calendar fact, not lookahead.

State is kept entirely on `self`; `ctx.history` is never indexed, so the
bounded window cannot raise.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """CVD continuation: follow unusually one-sided aggressive flow."""

    def __init__(
        self,
        *,
        interval: str = "15m",
        flow_bars: int = 16,
        hold_bars: int = 16,
        z_entry: float = 2.0,
        z_rearm: float = 1.0,
        min_imb: float = 0.15,
        min_obs: int = 96,
        stat_window: int = 960,
        stop_pct: float = 0.008,
        target_pct: float = 0.025,
        stale_bars: int = 4,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.flow_bars = flow_bars
        self.hold_bars = hold_bars
        self.z_entry = z_entry
        self.z_rearm = z_rearm
        self.min_imb = min_imb
        self.min_obs = min_obs
        self.stat_window = stat_window
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.stale_bars = stale_bars
        self.size = size

        # Rolling window of per-bar flow, for the 4h imbalance.
        self.deltas: deque[float] = deque(maxlen=flow_bars)
        self.volumes: deque[float] = deque(maxlen=flow_bars)

        # Rolling history of the same 4h statistic, for the regime z-score.
        self.imb_hist: deque[float] = deque(maxlen=stat_window)

        # Trade bookkeeping, all plain values -- never a context.
        self.bars_held = 0
        self.armed = True
        self.last_dir = 0
        self.stale = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]
        if bar.tick_count == 0:
            return

        self.deltas.append(float(bar.delta))
        self.volumes.append(float(bar.volume))

        total_vol = sum(self.volumes)
        imb = (sum(self.deltas) / total_vol) if total_vol > 0.0 else 0.0

        # z-score from what was seen *before* this bar, then extend the history.
        z = 0.0
        if len(self.imb_hist) >= self.min_obs:
            n = len(self.imb_hist)
            mean = sum(self.imb_hist) / n
            var = sum((x - mean) * (x - mean) for x in self.imb_hist) / n
            if var > 1e-12:
                z = (imb - mean) / (var ** 0.5)
        self.imb_hist.append(imb)

        # Re-arm the signal once it has relaxed or reversed direction.
        if (abs(z) < self.z_rearm
                or (self.last_dir != 0 and z * self.last_dir < 0.0)):
            self.armed = True

        # ---------------------------------------------------- open position
        if ctx.position != 0:
            self.bars_held += 1
            flipped = ((ctx.position > 0 and imb <= -self.min_imb)
                       or (ctx.position < 0 and imb >= self.min_imb))
            if self.bars_held >= self.hold_bars or flipped:
                self._flatten(ctx)
            return

        # ------------------------------------------------------------ flat
        self.bars_held = 0

        if ctx.working_order_ids:
            # Leftover bracket legs should be impossible (OCO), but never let
            # a stuck order block the strategy forever.
            self.stale += 1
            if self.stale > self.stale_bars:
                ctx.cancel_all()
                self.stale = 0
            return
        self.stale = 0

        if not ctx.actionable or not self.armed:
            return
        if len(self.deltas) < self.flow_bars:
            return
        if len(self.imb_hist) < self.min_obs:
            return

        if z >= self.z_entry and imb >= self.min_imb:
            direction = 1
        elif z <= -self.z_entry and imb <= -self.min_imb:
            direction = -1
        else:
            return

        price = ctx.last_price
        if price is None or price <= 0.0:
            return

        bar_ns = bar.close_ts - bar.open_ts
        if bar_ns <= 0:
            return
        # Do not open a position that would still be on across 00:00 UTC.
        if ctx.session_end - ctx.ts < self.hold_bars * bar_ns:
            return

        stop_off = price * self.stop_pct
        target_off = price * self.target_pct
        if stop_off <= 0.0 or target_off <= 0.0:
            return

        if direction > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="cvd-long",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="cvd-short",
            )
        if order_id is None:
            return  # action cap dropped it; nothing exists to manage
        self.armed = False
        self.last_dir = direction
        self.bars_held = 0

    # ------------------------------------------------------------------ exits

    def _flatten(self, ctx) -> None:
        """Pull the bracket legs, then close. Order of the two matters."""
        for order in ctx.working_orders:
            ctx.cancel(order.id)
        if ctx.position != 0:
            ctx.close()
        self.bars_held = 0

    # ----------------------------------------------------------------- events

    def on_fill(self, ctx) -> None:
        # A bracket leg (or a close) took us flat: the holding clock restarts.
        if ctx.fill.position == 0:
            self.bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        # No order ids are kept across callbacks, so an expiry leaves nothing
        # stale to act on. Nothing to do here.
        pass
