"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling is followed by higher
prices over the next comparable window; aggressive selling by lower prices.
This file implements that claim directly and does **not** fade it: the sign of
the signal is the sign of the position.

How it is expressed
-------------------
* One subscription, `1h` time bars. Each bar carries `buy_volume` and
  `sell_volume`, hence `bar.delta` = signed aggressor flow. Per
  `docs/agent/data.md` an order-flow feature at bar resolution does **not**
  need `ctx.subscribe("tick")`, and ticks cost roughly 5x the wall clock for
  the same information. `unknown_volume` is zero on this feed, so `bar.delta`
  is the exact signed flow and the "unknown aggressor" branch has nothing to
  decide.

* The measurement is a *volume-normalised* CVD over the last `lookback` bars:

      ratio = sum(bar.delta) / sum(bar.volume)   over the window,

  which lies in [-1, +1] and reads as "the fraction of aggressive volume that
  was on the buying side, minus the selling side". Normalising by the same
  window's total volume is the answer to the campaign's question of how to
  measure "large": 500 BTC of net buying means something different in a
  20k-BTC hour and a 200k-BTC hour, and the ratio removes exactly that.

* "Large" is adaptive rather than fixed. The entry threshold is the
  `q`-quantile of the last `rank_window` |ratio| observations, floored at
  `min_threshold`. A fixed cut-off on a 24/7 asset is really a bet on a
  volatility regime; a rolling quantile keeps the trade rate roughly constant
  across 2023 and 2026, which is one of the things the campaign says would
  make the answer interesting ("not one hour of the day", "present across
  ordinary sessions"). The quantile is recomputed every `recompute_every`
  bars, not every bar — sorting a few hundred numbers on every one of ~23k
  bars is needless work.

* Horizon: enter on a bar close, hold for exactly `hold` bars, exit at market.
  The claim is about "the next comparable window", so the horizon *is* the
  exit. No target: a target is a different claim (where the move stops), and
  a resting target would be filled by the continuation precisely when the
  continuation is strongest.

* Orders are market. The fill model is queue-pessimistic: a resting limit
  joins behind the displayed size and fills only when a trade reaches it, so
  the limit entry most likely to be filled is the one whose move never came.
  Paying the spread for a fill that actually happens is the cheaper error
  here, and 100 ms of latency on 1-lot orders is not meaningful slippage at
  bar scale.

* Costs are the binding constraint and are respected in the design rather than
  in a post-hoc filter: 10 bp round trip against a 6-hour BTC move whose
  standard deviation is on the order of 150-200 bp. That is a demanding but
  not absurd hurdle, and it is why the horizon is 6 hours rather than 30
  minutes — the fee is fixed while the size of the opportunity grows with the
  square root of the hold, so short horizons cannot pay for themselves even
  if the effect is there.

* Risk: a wide catastrophe stop at `stop_bps` of adverse excursion from the
  average entry, checked on bar closes. It is insurance, not the thesis: at
  4% it is roughly 2-3 sigma of a 6-hour move and should almost never bind.
  It is checked in code rather than placed as an engine stop order so that no
  bracket-side validation, DAY expiry or OCO event can end the run, and no
  order id is ever held across callbacks where it could go stale.

* Position size is one lot (0.0001 BTC). The result is scale-free in the size
  — fees, funding and P&L are all linear in it — so the smallest tradable
  size keeps the trial's risk honest while the statistics accumulate.

What is deliberately absent: a tick subscription (wall-clock cost), any fade
(the campaign names the direction), a book-based input (not available for
this instrument), a regime model, and any second instrument.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """CVD continuation: follow large volume-normalised aggressor imbalance."""

    def __init__(self, interval="1h", lookback=6, hold=6, q=0.85,
                 min_threshold=0.02, rank_window=240, min_rank=60,
                 recompute_every=24, size=1, stop_bps=400.0):
        self.interval = interval
        self.lookback = lookback        # bars of aggressor flow in the signal
        self.hold = hold                # bars held before the horizon exit
        self.q = q                      # quantile of |ratio| that counts as large
        self.min_threshold = min_threshold
        self.rank_window = rank_window  # how many |ratio| observations to rank
        self.min_rank = min_rank        # observations needed before trading
        self.recompute_every = recompute_every
        self.size = size
        self.stop_bps = stop_bps        # catastrophe stop, basis points

        self._ranks = deque(maxlen=rank_window)
        self._threshold = None
        self._bars = 0
        self._held = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx):
        history = ctx.history(self.interval)
        if len(history) < self.lookback:
            return

        # Volume-normalised cumulative delta over the window. last() is bounded
        # by what exists rather than raising, and we have already checked len().
        window = history.last(self.lookback)
        volume = 0
        delta = 0
        for bar in window:
            volume += bar.volume
            delta += bar.delta
        if volume <= 0:
            return
        ratio = delta / volume

        # Keep the ranking up to date during warmup too: those bars are real,
        # only the orders would not be, and arriving at the first actionable
        # bar with no observation of "large" would waste the warmup.
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

        self._held = 0
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
        # Flat again after an exit fill: restart the horizon clock for the next
        # entry. An entry fill leaves the counter where it is (zero).
        if ctx.fill.position == 0:
            self._held = 0

    def on_lifecycle(self, ctx):
        pass
