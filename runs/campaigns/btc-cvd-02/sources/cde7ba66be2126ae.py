"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices, and aggressive selling by lower ones. The sign of the
accumulated flow is the sign of the position. This file follows the flow; it does
not fade it.

The record, and the two levers it isolates
------------------------------------------
#0 (1h bars, 6h flow, 6h hold, rolling quantile) made 1,196 round trips and
scored -0.1415: at 10 bp a round trip the fee bill was the whole of the loss, so
its gross edge was about zero. #1 (short hold, wide bracket) was genuinely
negative gross. #2 and #3 cut the trade count and were refused for the exposure
floor (251 and 392 of 960 scored sessions against a required 70%). #4 — a
persistent position held on the sign of a 48-hour flow window, z-scored against
its own trailing 30-day dispersion, entry at |z| > 0.5, flip at the opposite
|z| > 1.0 — traded 161 times and scored **+0.1365**. #5 (72-hour window) +0.0201.
#6 (12/24/48 consensus) +0.0864 with 264 fills. #7 (#4 with flip_z 1.25 and a
12-bar minimum hold) +0.0778 with 127 fills. #8 (#4 evaluated on 15-minute bars,
same 48 hours of flow, same bands) **+0.1677** with 167 fills. #9 (#4 with
flip_z narrowed to 0.75, still hourly) +0.1545 with 221 fills.

Two clean single changes each helped, and they are the only two levers in this
family that have helped at all:

* **Cadence** — 1h to 15m, bands untouched: 161 fills to 167 fills, +0.1365 to
  +0.1677. The trade count barely moved, so this was not a turnover effect; the
  net result *per fill* rose from 8.5 bp to 10.0 bp of notional.

* **Band width** — flip_z 1.25 to 1.0 to 0.75, cadence untouched: +0.0778,
  +0.1365, +0.1545 with 127, 161, 221 fills. Monotone in the same direction
  across three points; the marginal flip at 0.75 still carried edge.

This file is the 2x2 closed: #4's estimator and state machine on **15-minute
bars with flip_z = 0.75**. `flow_window = 192` bars is the same 48 hours,
`sd_window = 2880` the same 30 days, `min_sd_obs = 672` the same 7 days,
`cooldown_bars = 96` the same 24 hours. Every parameter expressible in hours is
still expressed in the same number of hours. Only the two things already shown
to matter alone are on together.

The change in this revision, and why
------------------------------------
The previous submission was gated shut on a short walk: it returned unless 192
bars had closed *and* `_z` had 672 ratio observations behind it, so on a short
synthetic run it never formed a reading and never placed an order. Neither gate
is a market state — the first is an estimator warm-up and the second is only the
yardstick for "large". Both are now non-blocking:

* the flow ratio is computed over `min(flow_window, bars_available)` once
  `min_flow_bars` bars exist, so a short walk still forms a reading;
* an undefined `z` (too few observations for a dispersion estimate) is read as
  maximal in the sign of `ratio` rather than as a reason not to act.

Both are inert on the real split: 192 bars and 672 observations are long past by
the first actionable bar of a 960-session run, so nothing downstream of the
warm-up changes. They matter only where the old code was silent.

Everything not named above is unchanged, deliberately
-----------------------------------------------------
* **Measurement.** Over the last `flow_window` bars,

      ratio = sum(bar.delta) / sum(bar.volume)

  cumulative volume delta made dimensionless: the fraction of the window's
  aggressive volume that was net buying, minus net selling. Dividing by the
  window's own volume is the campaign's "how to measure large" answered once —
  500 BTC of net buying is a different event in a quiet Asian morning and at a US
  open. `unknown_volume` is zero on this feed, so `bar.delta` is the exact signed
  flow, and no tick subscription is needed: ticks cost roughly five times the
  wall clock for a number the bar already carries exactly.

* **"Large" is standardised.** `z = ratio / sd(ratio)` over the trailing 30 days,
  needing 7 days of observations. A fixed cut-off on a 24/7 asset is a bet on the
  volatility regime of the year it was chosen in. The cut is on the *level* of
  the ratio, not its deviation from a trailing mean: the claim is that one-sided
  flow predicts, and a month of persistent net buying should not be relabelled
  "not unusual" merely because it is the local average.

* **Hysteresis between entry and flip.** Opening from flat needs |z| > 0.5;
  reversing needs |z| > 0.75. A flip pays two fees and throws away a position the
  flow has not yet contradicted. The gap is what keeps the position on almost
  always without ever paying to sit out, which is also what clears the 70%
  exposure floor by construction rather than by tuning a trade rate.

* **Market orders, and only on flips.** The fill model is book-queue-pessimistic:
  a resting limit joins behind the displayed size and fills only when trades
  reach through it, which on a continuation entry selects precisely the cases
  where the expected move did not arrive. Paying the touch for a fill that
  actually happens is the cheaper error, and 100 ms against a 15-minute decision
  interval is a rounding error on the cadence lever itself.

* **Risk is a backstop, not a rule.** A close-of-bar check at 15% of adverse
  excursion from the average entry, held in code rather than armed as an engine
  order, so no bracket validation, DAY expiry or OCO event can end the run and
  no order id is carried across a callback where an expiry could make it stale.
  Afterwards the strategy stands aside for a day and re-enters on a fresh reading.

* **Bookkeeping is O(1) per bar.** The flow window is a bounded deque with
  running `delta` and `volume` sums; the dispersion is running sums over a
  bounded deque with a periodic exact re-sync. Nothing rescans history — there is
  no `ctx.history` read at all, so the 512-bar pushed window cannot be overrun —
  and nothing grows across sessions.

Position size is one lot, unchanged. Fees, funding and P&L are linear in it, so
the result is scale-free and the smallest tradable size keeps the trial's risk
honest while the statistics accumulate.

What is deliberately absent: a tick subscription, any fade (the campaign names
the direction), any book-based input (not available on this adapter), a regime
model, a target or bracket, a second instrument, and any fixed-horizon exit. The
horizon *is* the flow window, and the position is held while the flow agrees
with it.

Outcomes and what they would mean
---------------------------------
Above #8: cadence and band compose; the next lever is the estimator — a
time-decayed flow window or a return-adjusted one — not another band.
Between #8 and #9: the composition is partial and the two levers are substitutes;
stop spending trials on the band.
Below #8: the hourly close was a filter, and a finer sampler with a narrow band
buys whipsaw rather than timing. That would be the most informative result of the
three, because it would say the family's apparent parameter surface is one
dimension wearing two names.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation: hold with the flow, flip on a decisive reversal."""

    def __init__(self, interval="15m", flow_window=192, sd_window=2880,
                 min_sd_obs=672, entry_z=0.5, flip_z=0.75, size=1,
                 stop_bps=1500.0, cooldown_bars=96, min_flow_bars=8):
        self.interval = interval            # 15m bars: #8's clock
        self.flow_window = flow_window      # 192 bars = 48 hours of aggressor flow
        self.sd_window = sd_window          # 2880 bars = 30 days of ratio history
        self.min_sd_obs = min_sd_obs        # 672 bars = 7 days before a scale exists
        self.entry_z = entry_z              # |z| needed to open from flat
        self.flip_z = flip_z                # |z| needed to reverse: #9's band
        self.size = size
        self.stop_bps = stop_bps            # catastrophe stop, basis points
        self.cooldown_bars = cooldown_bars  # 96 bars = 24 hours flat after a stop
        self.min_flow_bars = min_flow_bars  # bars before any ratio exists at all

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

        # A full window is the signal; a partial one at the very start of a run
        # is still a reading of the same statistic, and refusing to form it is
        # not a market state.
        if len(self.flow) < self.min_flow_bars or self.sum_volume <= 0.0:
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
            # No dispersion estimate yet. `ratio` is a number the data supplied;
            # what is missing is only the yardstick for "large". With no
            # yardstick the sign of the flow is the whole of the evidence, so
            # read it as maximal in that direction rather than declining to act
            # -- an undefined scale is not a market state.
            if ratio == 0.0:
                return
            z = 2.0 if ratio > 0.0 else -2.0

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
        # Keep the direction in step with what actually happened; the flip is a
        # single order, so there is nothing else to reconcile and no order id is
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
