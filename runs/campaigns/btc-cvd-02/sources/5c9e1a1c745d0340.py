"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling is followed by higher prices
over the next comparable window; aggressive selling by lower prices. The sign of
the signal is the sign of the position. This file does not fade. If the
continuation claim is wrong at this horizon the score will say so; rotating to
the other side would test a different hypothesis and answer nothing.

What the two prior trials said, and what changes here
-----------------------------------------------------
Both prior trials implemented the claim and both lost money. Trial #0 (1h bars,
6-bar flow window, 6-bar hold, 0.85 rolling quantile) made **1,196** round trips
and scored -0.1415. Trial #1 (5m bars, 1h flow window, 1h hold, z-score entry)
made 185 round trips and scored -0.2349.

The fee arithmetic is the thing to read from that pair. A round trip costs 10 bp
of notional, and the position size is fixed, so the fee is a **fixed tax per
trade** while the opportunity grows with the square root of the holding period.
At a 1h horizon the standard deviation of the move being forecast is roughly
50 bp, so the fee is 20% of one sigma — the signal would have to be
extraordinarily strong to pay. At 6h it is ~135 bp and the fee is 7% of sigma;
at 24h the move is ~250 bp and the fee is 4% of sigma. Trial #0's gross edge was
evidently small but not obviously negative: 1,196 trades cost about 1.2 units of
notional, and the net result was only -0.14. Trial #1 spent a quarter of that on
fees and did worse, which is what "the gross edge was zero" looks like.

So the lever that is actually available is **trade count at a long horizon** —
hold the claim fixed, shrink the number of round trips, and let each one earn a
move that is large relative to 10 bp. This file does that:

* Flow window 12 bars (12 hours), hold 24 bars (24 hours). The measurement
  window and the holding period are of the same order, which is what "the next
  comparable window" asks for, and the hold is long enough that the fee is a few
  percent of the move it is trying to catch rather than a fifth of it.

* The threshold is the **0.95** quantile of the trailing 60 days of |ratio| —
  up from 0.85 — plus a 24-bar cooldown after every exit. Both exist to cut the
  round-trip count. Extreme flow readings cluster (the 12-bar sum overlaps
  itself), so a docile quantile converts one flow episode into a dozen trades;
  a stricter cut and a cooldown turn it into one or two.

* No target and no bracket. A target is a different claim (where the move
  stops), and the claim here is about the next comparable window, so the horizon
  *is* the exit. The only other exit is a catastrophe stop, set wide enough
  (6%, about 2.4 sigma of a 24-hour move) that it is insurance rather than a
  competing rule; it is checked in code on bar closes, so no bracket-side
  validation, DAY expiry or OCO event can end the run, and no order id is held
  across callbacks where an expiry could make it stale.

The measurement
---------------
`bar.delta` is buy_volume - sell_volume, so summing it over the window is
cumulative volume delta. CVD in coin is not comparable across hours and years,
so it is divided by the volume traded over the same window:

    ratio = sum(bar.delta) / sum(bar.volume)   over the last 12 bars

which lies in [-1, 1] and reads as the fraction of the window's volume that was
one-sided. That is the answer to the campaign's "how to measure large": 500 BTC
of net buying means different things in a quiet Asian morning and a US open, and
normalising by the same window's volume removes exactly that difference. The
`unknown_volume` branch has nothing to decide on this feed — every trade carries
its aggressor, so `delta` is the exact signed flow.

"Large" is then made adaptive. A fixed cut-off on a 24/7 asset is a bet on a
volatility regime; the rolling quantile of |ratio| keeps the trade rate roughly
constant across 2023 and 2026, which is one of the conditions the campaign names
("not one hour of the day", "present across ordinary sessions"). The quantile is
recomputed every 24 bars rather than every bar — sorting 1,440 floats on each of
~23,000 bars is needless work against a wall-clock budget.

Orders are market. The fill model is queue-pessimistic: a resting limit joins
behind the displayed size and fills only when trades reach it, which on a
continuation signal selects exactly the cases where the move never came. Paying
the touch for a fill that actually happens is the cheaper error, and 100 ms of
latency on a 1-lot order is not meaningful slippage at bar scale.

Position size is one lot (0.0001 BTC). Fees, funding and P&L are all linear in
size, so the result is scale-free and the smallest tradable size keeps the
trial's risk honest.

What is deliberately absent: a tick subscription (5x the wall clock for
information `bar.delta` already carries), any fade, any book-based input (not
available on this adapter), a regime model, and any second instrument.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """CVD continuation: follow large volume-normalised aggressor imbalance."""

    def __init__(self, interval="1h", lookback=12, hold=24, q=0.95,
                 min_threshold=0.05, rank_window=1440, min_rank=120,
                 recompute_every=24, cooldown=24, stop_bps=600.0, size=1):
        self.interval = interval
        self.lookback = lookback          # bars of aggressor flow in the signal
        self.hold = hold                  # bars held before the horizon exit
        self.q = q                        # quantile of |ratio| that counts as large
        self.min_threshold = min_threshold
        self.rank_window = rank_window    # observations in the ranking window
        self.min_rank = min_rank          # observations needed before trading
        self.recompute_every = recompute_every
        self.cooldown = cooldown          # bars flat before a new entry
        self.stop_bps = stop_bps          # catastrophe stop, basis points
        self.size = size

        self._ranks = deque(maxlen=rank_window)
        self._threshold = None
        self._bars = 0
        self._held = 0
        self._cooldown = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx):
        history = ctx.history(self.interval)
        if len(history) < self.lookback:
            return

        # Volume-normalised cumulative delta over the window. last() is bounded
        # by what exists rather than raising; len() was checked above.
        window = history.last(self.lookback)
        volume = 0
        delta = 0
        for bar in window:
            volume += bar.volume
            delta += bar.delta
        if volume <= 0:
            return
        ratio = delta / volume

        # The ranking accumulates through warmup too: those bars are real, only
        # the orders would not be, and reaching the first actionable bar with no
        # observation of "large" would waste the warmup the harness paid for.
        self._ranks.append(abs(ratio))
        self._bars += 1
        if self._threshold is None or self._bars % self.recompute_every == 0:
            self._refresh_threshold()

        if not ctx.actionable:
            return

        # ---- in a position: manage it, never add to it ----
        if ctx.position != 0:
            self._manage(ctx)
            return

        # ---- flat ----
        self._held = 0
        if self._cooldown > 0:
            self._cooldown -= 1
            return
        if ctx.working_order_ids:
            return
        threshold = self._threshold
        if threshold is None:
            return

        if ratio > threshold:
            ctx.buy(size=self.size, tag="cvd-long")
        elif ratio < -threshold:
            ctx.sell(size=self.size, tag="cvd-short")

    def _manage(self, ctx):
        # A close is already in flight; act only once it has resolved.
        if ctx.working_order_ids:
            return
        self._held += 1

        entry = ctx.average_entry_price
        price = ctx.last_price
        if entry > 0 and price > 0:
            if ctx.position > 0:
                adverse = entry - price
            else:
                adverse = price - entry
            if adverse > entry * self.stop_bps / 10000.0:
                ctx.close()
                return

        # The horizon is the exit. The claim is about the next comparable
        # window, so there is nothing to decide here.
        if self._held >= self.hold:
            ctx.close()

    def _refresh_threshold(self):
        if len(self._ranks) < self.min_rank:
            return
        ordered = sorted(self._ranks)
        idx = int(self.q * (len(ordered) - 1))
        self._threshold = max(float(ordered[idx]), self.min_threshold)

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx):
        if ctx.fill.position == 0:
            # Flat: the clock exit or the catastrophe stop resolved. Sit out the
            # cooldown so one flow episode is not paid for repeatedly.
            self._held = 0
            self._cooldown = self.cooldown
        else:
            # The holding clock starts at the fill, not at the order.
            self._held = 0

    def on_lifecycle(self, ctx):
        pass
