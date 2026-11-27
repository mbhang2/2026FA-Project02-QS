"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices, and aggressive selling by lower ones. The sign of the
accumulated flow is the sign of the position. This file follows the flow; it does
not fade it. If continuation is wrong at this horizon the score says so, and
rotating to the other side would answer a different question.

Where the trials have landed
----------------------------
#0 (1h bars, 6h flow, 6h hold, rolling quantile) made 1,196 fills and scored
-0.1415; at 10 bp per flip the fee bill was the whole of the loss, so its gross
edge was about zero. #1 (short hold, wide bracket) lost more than fees explain.
#2 and #3 cut the trade count and were refused for the exposure floor (251 and
392 of 960 scored sessions against a registered 70%). #4 — a persistent position
on the sign of a 48-hour flow ratio, entry at |z| > 0.5, flip at |z| > 1.0 —
traded 161 fills and scored +0.1365. #5 (72-hour window) +0.0201. #6 (12/24/48
consensus, entry 0.4, 24-bar minimum hold) +0.0864. #7 (wider flip band 1.25, a
12-bar minimum hold) +0.0778. #8 — #4 evaluated on 15-minute bars instead of
hourly, every other parameter held fixed — traded 167 fills and scored +0.1677,
the best so far. #9 — #4 with the flip band narrowed from 1.0 to 0.75, hourly —
traded 221 fills and scored +0.1545.

What #8 and #9 separately establish
-----------------------------------
#8 improved on #4 by +0.031 while adding only 6 fills (161 → 167). The 48-hour
flow ratio is smooth; sampling it four times as often changes *when* a crossing
is acted on, not *which* crossings occur. The gain is the decision lag #4 left on
the table, and it is a durable improvement rather than a trade-count change.

#9 improved on #4 by +0.018 while adding 60 fills (161 → 221). The extra flips
cost about 0.060 in score at 10 bp round trips, so their gross contribution was
about +0.078 — roughly 13 bp per extra flip, against a 10 bp round trip. The
marginal flip is profitable, but only just. That is the reason to stop at 0.75
rather than push the band lower: at 1.0 the flip band was leaving money on the
table; at 0.75 the remaining margin is thin enough that a further narrowing is a
different and more expensive experiment.

The single change: #8's clock with #9's band
--------------------------------------------
This file is #8's estimator and state machine — 15-minute bars, a 48-hour flow
window, a 30-day standardisation of the raw ratio, entry at |z| > 0.5, market
orders, one lot, the same 15% catastrophe stop — with one number changed from
#8: the flip threshold falls from 1.0 to 0.75. Both changes have been tested in
isolation against the same #4 baseline and both helped. Their interaction is the
only open question, and it is a small one: at 15 minutes the 48-hour ratio is
nearly the same path #9 saw hourly (6 extra flips at the 1.0 band, measured), so
the flip count should land near #9's 221, not far above it. If the 15-minute
clock makes the marginal flips earlier and therefore better, this should score
above #8; if the extra sampling adds only noise at the 0.75 band, it should land
between #8 and #9. A materially lower score would say the narrow band and the
finer clock are not additive, which is worth knowing before another band is
tried.

The rest is #8, unchanged, and deliberately so
---------------------------------------------
* **Measurement.** Over the last `flow_window` bars,

      ratio = sum(bar.delta) / sum(bar.volume)

  which is cumulative volume delta made dimensionless: the fraction of the
  window's aggressive volume that was net buying, minus net selling. Dividing by
  the window's own volume is the campaign's "how to measure large" answered
  once: 500 BTC of net buying is a different event in a quiet Asian morning and
  at a US open. `unknown_volume` is zero on this feed — every trade carries its
  aggressor — so `bar.delta` is the exact signed flow, there is no unknown
  branch to decide, and no tick subscription is needed: ticks cost roughly five
  times the wall clock for a number the bar already carries exactly.

* **"Large" is standardised.** `z = ratio / sd(ratio)` over the trailing 30 days,
  needing 7 days of observations. A fixed cut-off on a 24/7 asset is a bet on the
  volatility regime of the year it was chosen in; standardising by the signal's
  own recent dispersion keeps a band meaning the same thing in 2023 and in 2026.
  The cut is on the *level* of the ratio, not on its deviation from a trailing
  mean: the claim is that one-sided flow predicts, and a month of persistent net
  buying should not be relabelled "not unusual" merely because it happens to be
  the local average.

* **Hysteresis between entry and flip.** Opening from flat (the first actionable
  bar, or after a stop) needs |z| > 0.5; reversing needs |z| > 0.75. A flip pays
  two fees and throws away a position the flow has not yet contradicted, so it
  demands a more decisive reversal than an ordinary entry. The gap is what keeps
  the position on almost always without ever paying to sit out.

* **Market orders, and only on flips.** The fill model is book-queue-pessimistic:
  a resting limit joins behind the displayed size and fills only when trades
  reach through it, which on a continuation entry selects precisely the cases
  where the expected move did not arrive. Paying the touch for a fill that
  actually happens is the cheaper error, and 100 ms of latency on a one-lot
  order — 0.0001 BTC — is not meaningful slippage at this scale.

* **Risk is a backstop, not a rule.** A close-of-bar check at 15% of adverse
  excursion from the average entry, held in code rather than armed as an engine
  order, so no bracket validation, DAY expiry or OCO event can end the run and no
  order id is carried across a callback where an expiry could make it stale. It
  is meant to be crossed by a crash and not by ordinary noise; afterwards the
  strategy stands aside for a day and re-enters on a fresh `entry_z` reading.

* **Bookkeeping is O(1) per bar.** The flow window is a bounded deque with
  running `delta` and `volume` sums; the scale is running sums over a bounded
  deque with a periodic exact re-sync. Nothing rescans history — there is no
  `ctx.history` read at all, so the 512-bar pushed window cannot be overrun — and
  nothing grows across sessions.

Position size is one lot, and it is unchanged. One lot is 0.0001 BTC; fees,
funding and P&L are all linear in it, so the result is scale-free and the smallest
tradable size keeps the trial's risk honest while the statistics accumulate.
Scaling up would buy nothing except a bigger number in whatever units the score
happens to be expressed in, and would make this trial incomparable with the
others in the family.

What is deliberately absent: a tick subscription, any fade (the campaign names
the direction), any book-based input (not available on this adapter), a regime
model (the standardisation is a scale, not a state), a second instrument, a
target or bracket (a different claim — where the move stops), and any
fixed-horizon exit. The horizon *is* the flow window, and the position is held
while the flow agrees with it.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation: hold with the flow, flip on a 0.75-sd reversal."""

    def __init__(self, interval="15m", flow_window=192, sd_window=2880,
                 min_sd_obs=672, entry_z=0.5, flip_z=0.75, size=1,
                 stop_bps=1500.0, cooldown_bars=96):
        self.interval = interval            # 15m bars: #8's clock
        self.flow_window = flow_window      # 192 bars = 48 hours of aggressor flow
        self.sd_window = sd_window          # 2880 bars = 30 days of ratio history
        self.min_sd_obs = min_sd_obs        # 672 bars = 7 days before a scale exists
        self.entry_z = entry_z              # |z| needed to open from flat
        self.flip_z = flip_z                # |z| needed to reverse an open position
        self.size = size
        self.stop_bps = stop_bps            # catastrophe stop, basis points
        self.cooldown_bars = cooldown_bars  # 96 bars = 24 hours flat after a stop

        # Rolling flow window with running sums: (delta, volume) per closed bar.
        self.flow = deque()
        self.sum_delta = 0.0
        self.sum_volume = 0.0

        # Rolling distribution of the raw ratio: running sums, bounded deque.
        self.ratios = deque()
        self.sr = 0.0
        self.sr2 = 0.0
        self._bars = 0

        self.dir = 0                        # last known position direction
        self.cooldown = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx):
        # One subscription, but ask rather than assume: a bar from another
        # interval closing at this instant must not be mistaken for ours.
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # --- flow window, O(1) ----------------------------------------------
        if len(self.flow) >= self.flow_window:
            old_delta, old_volume = self.flow.popleft()
            self.sum_delta -= old_delta
            self.sum_volume -= old_volume
        delta = float(bar.delta)
        volume = float(bar.volume)
        self.flow.append((delta, volume))
        self.sum_delta += delta
        self.sum_volume += volume

        if len(self.flow) < self.flow_window or self.sum_volume <= 0.0:
            return
        ratio = self.sum_delta / self.sum_volume

        # The scale accumulates through warmup as well: those bars are real, only
        # the orders would not be, and reaching the first actionable bar with no
        # idea what "large" means here would waste the warmup.
        self._push_ratio(ratio)
        self._bars += 1
        if self._bars % 2048 == 0:
            self._resync()

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return

        if self.cooldown > 0:
            self.cooldown -= 1

        position = ctx.position
        if position > 0:
            self.dir = 1
        elif position < 0:
            self.dir = -1
        else:
            self.dir = 0

        # ---- catastrophe stop: insurance, checked on closes -----------------
        if position != 0:
            entry = ctx.average_entry_price
            price = ctx.last_price
            if entry > 0.0 and price > 0.0:
                if position > 0:
                    adverse = entry - price
                else:
                    adverse = price - entry
                if adverse > entry * self.stop_bps / 10000.0:
                    ctx.close()
                    self.dir = 0
                    self.cooldown = self.cooldown_bars
                    return

        z = self._z(ratio)
        if z is None:
            return

        # ---- already positioned: hold unless the flow has reversed hard ------
        if self.dir == 1:
            if z < -self.flip_z:
                # One order takes the book from +position to -size: no bar spent
                # flat, and no half-applied flip if only the close had filled.
                if ctx.sell(size=position + self.size,
                            tag="cvd-flip-short") is not None:
                    self.dir = -1
            return
        if self.dir == -1:
            if z > self.flip_z:
                if ctx.buy(size=(-position) + self.size,
                           tag="cvd-flip-long") is not None:
                    self.dir = 1
            return

        # ---- flat: open with the sign of the flow ----------------------------
        if self.cooldown > 0:
            return
        if z > self.entry_z:
            if ctx.buy(size=self.size, tag="cvd-long") is not None:
                self.dir = 1
        elif z < -self.entry_z:
            if ctx.sell(size=self.size, tag="cvd-short") is not None:
                self.dir = -1

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx):
        # Keep the direction in step with what actually happened; the flip order is
        # a single order, so there is nothing else to reconcile and no order id is
        # held across callbacks for an expiry to make stale.
        position = ctx.fill.position
        if position > 0:
            self.dir = 1
        elif position < 0:
            self.dir = -1
        else:
            self.dir = 0

    def on_lifecycle(self, ctx):
        pass

    # ---------------------------------------------------------------- helpers

    def _push_ratio(self, value):
        if len(self.ratios) >= self.sd_window:
            old = self.ratios.popleft()
            self.sr -= old
            self.sr2 -= old * old
        self.ratios.append(value)
        self.sr += value
        self.sr2 += value * value

    def _resync(self):
        self.sr = 0.0
        self.sr2 = 0.0
        for value in self.ratios:
            self.sr += value
            self.sr2 += value * value
        d = 0.0
        v = 0.0
        for bar_delta, bar_volume in self.flow:
            d += bar_delta
            v += bar_volume
        self.sum_delta = d
        self.sum_volume = v

    def _z(self, ratio):
        n = len(self.ratios)
        if n < self.min_sd_obs:
            return None
        mean = self.sr / n
        var = self.sr2 / n - mean * mean
        if var <= 0.0:
            return None
        sd = math.sqrt(var)
        if sd < 1e-12:
            return None
        return ratio / sd
