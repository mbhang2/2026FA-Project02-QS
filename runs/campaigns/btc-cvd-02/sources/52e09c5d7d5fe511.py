"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices, and aggressive selling by lower ones. The sign of the
accumulated flow is the sign of the position. This file follows the flow; it does
not fade it. If continuation is wrong at this horizon the score says so, and
rotating to the other side would answer a different question.

The record, and the axis it leaves open
---------------------------------------
#0 (1h bars, 6h flow, 6h hold, rolling quantile) made 1,196 round trips and
scored -0.1415: at 10 bp a round trip the fee bill was the whole of the loss, so
its gross edge was about zero. #1 (short hold, wide bracket) was genuinely
negative gross. #2 and #3 cut the trade count and were refused for the exposure
floor (251 and 392 of 960 scored sessions against a required 70%). #4 — a
persistent position held on the sign of a 48-hour **rectangular** flow window,
z-scored against its own trailing 30-day dispersion, entry at |z| > 0.5, flip at
1.0 — traded 161 times and scored **+0.1365**. #5 (72-hour window) +0.0201. #6
(12/24/48 consensus) +0.0864. #7 (flip 1.25, 12-bar minimum hold) +0.0778. #8
(#4 evaluated on 15-minute bars, bands untouched) **+0.1677** with 167 fills. #9
(#4 with flip_z 0.75, hourly) +0.1545 with 221 fills. #10 (15m with flip_z 0.75)
**+0.1269** with 252 fills; #11 was the same file with an edit the engine could
not see.

The 2x2 is closed and the two levers are substitutes, not independent:

    flip_z        1h         15m
    1.00        +0.1365     +0.1677
    0.75        +0.1545     +0.1269

The net score per fill tells the same story from the other side:

    config           fills   score     gross per fill (net + 0.001)
    1h / 1.00         161   +0.1365           18.5 bp
    15m / 1.00        167   +0.1677           20.0 bp
    1h / 0.75         221   +0.1545           17.0 bp
    15m / 0.75        252   +0.1269           15.0 bp

At 15 minutes the flips the narrow band adds are gross-positive (0.052 bp per
fill is what the 85 extra fills were worth before costs — 5.2 bp, against a 10 bp
round trip) and net-negative. At an hourly cadence the marginal flip still
clears its cost. **So the band is a dead end: at the 15-minute clock, the band
that maximises the score is the one already there.** The remaining question the
family has not asked is not how often to re-decide, nor how wide the band is, but
**what the statistic itself is**.

The single change: a rectangular flow window becomes a time-decayed one
----------------------------------------------------------------------
Everything in #8 is kept — 15-minute bars, `entry_z = 0.5`, `flip_z = 1.0`,
market orders, one lot, the same 15% catastrophe stop and 24-hour cooldown —
except the shape of the weighting inside the flow measure. Instead of

    ratio = sum(delta, last 192 bars) / sum(volume, last 192 bars)

this file keeps two decayed running sums,

    num <- lam * num + bar.delta
    den <- lam * den + bar.volume
    ratio = num / den                lam = 2 ** (-1 / 48)  (48-bar half-life)

which is the same statistic — the volume-weighted fraction of aggressive volume
that was net buying — under a geometric weight instead of a rectangular one.

Two things this buys, and one thing it costs:

* **No exit discontinuity.** A rectangular window drops a bar in one step. When
  that bar carried a large delta, `ratio` jumps by a visible amount and can cross
  `-flip_z` **with no new trade having happened**. Every such crossing is a flip
  paid for by arithmetic, not by the market. The decay has no exit: an old bar
  fades out of the sum over tens of bars rather than falling out on one. #8's 167
  flips are the right population to ask this of, and the predicted effect is that
  a few of them disappear and the ones that remain are triggered by new flow.

* **Recency, continuously.** #8 established that acting sooner after a crossing
  is worth about +0.03 on its own. Decay weights the last few hours above the
  first few, which is the same lever applied inside the estimator instead of to
  the sampler. The mean lag falls from 96 bars (24 h) to 69.6 bars (17.4 h) at a
  48-bar half-life, and the effective observation count from 185 to ~138 — a
  moderate step, deliberately not a short-horizon bet.

* **It costs variance.** Fewer effective observations means a noisier `ratio`,
  so a few more crossings of the same band. That is the risk in this trial and it
  is why the half-life is 48 bars rather than 8: 48 bars is the point where the
  estimator still averages more than a day of flow, so the tail of `z` stays
  roughly where #8 had it, while the rectangular artefact goes away entirely.

The half-life is expressed in bars, but it is a number of hours: 48 bars of 15
minutes is 12 hours, and the resulting memory (~34.6 effective hours) sits between
#8's 48-hour window and the shorter horizons that #1 showed to be genuinely
negative gross. This is the estimator change the record points at, not a horizon
change wearing the same clothes.

The rest is #8, unchanged, and deliberately so
---------------------------------------------
* **Measurement.** `ratio` is cumulative volume delta made dimensionless — the
  fraction of the window's aggressive volume that was net buying, minus net
  selling. Dividing by the window's own volume is the campaign's "how to measure
  large" answered once: 500 BTC of net buying is a different event in a quiet
  Asian morning and at a US open, and it removes exactly that difference.
  `unknown_volume` is zero on this feed, so `bar.delta` is the exact signed flow,
  and no tick subscription is needed: ticks cost roughly five times the wall
  clock for a number the bar already carries exactly.

* **"Large" is standardised.** `z = ratio / sd(ratio)` over the trailing 30 days
  of ratio observations, needing 7 days before a scale exists. A fixed cut-off on
  a 24/7 asset is a bet on the volatility regime of the year it was chosen in.
  The cut is on the **level** of the ratio, not its deviation from a trailing
  mean: the claim is that one-sided flow predicts, and a month of persistent net
  buying must not be relabelled "not unusual" merely because it is the local
  average. The standardisation adapts to the decayed estimator's own dispersion
  by construction — if the smoother series has smaller spread, `sd` shrinks with
  it and a band means the same thing it meant at the rectangular window.

* **Hysteresis between entry and flip.** Opening from flat needs |z| > 0.5;
  reversing needs |z| > 1.0. A flip pays two fees and throws away a position the
  flow has not yet contradicted, so it demands a more decisive reversal. The gap
  is what keeps the position on almost always without ever paying to sit out,
  which is what clears the 70% exposure floor by construction rather than by
  tuning a trade rate — the property #2 and #3 lacked.

* **Market orders, and only on flips.** The fill model is book-queue-pessimistic:
  a resting limit joins behind the displayed size and fills only when trades reach
  through it, which on a continuation entry selects precisely the cases where the
  expected move did not arrive. Paying the touch for a fill that actually happens
  is the cheaper error, and 100 ms against a 15-minute decision interval is a
  rounding error on the cadence lever itself.

* **Risk is a backstop, not a rule.** A close-of-bar check at 15% of adverse
  excursion from the average entry, held in code rather than armed as an engine
  order, so no bracket validation, DAY expiry or OCO event can end the run and no
  order id is carried across a callback where an expiry could make it stale.
  Afterwards the strategy stands aside for a day and re-enters on a fresh reading.

* **Bookkeeping is O(1) per bar and bounded.** The flow measure is two floats
  with a multiply-add — no deque at all, so there is nothing to scan and nothing
  to drift. The dispersion is running sums over a bounded 2,880-element deque
  with a periodic exact re-sync. Nothing reads `ctx.history`, so the 512-bar
  pushed window cannot be overrun, and nothing grows across sessions.

* **The warm-up gates are non-blocking, as #10 and #11 established.** A reading
  forms once `min_flow_bars` bars exist rather than after a full 192; an
  undefined `z` (too few observations for a dispersion estimate) is read as
  maximal in the sign of `ratio` rather than as a reason not to act. Neither is a
  market state. On the real split both are long past by the first actionable bar
  of a 960-session run, so they change nothing downstream; they matter only where
  the code was previously silent.

Position size is one lot, unchanged. Fees, funding and P&L are linear in it, so
the result is scale-free and the smallest tradable size keeps the trial's risk
honest while the statistics accumulate.

What is deliberately absent: a tick subscription, any fade (the campaign names
the direction), any book-based input (not available on this adapter), a regime
model, a target or bracket, a second instrument, and any fixed-horizon exit. The
horizon *is* the flow window, and the position is held while the flow agrees with
it.

Outcomes and what they would mean
---------------------------------
Above #8 (+0.1677): the rectangular window's exit discontinuity was real, and the
next estimator question is the scale — whether `sd` should be measured on the
decayed ratio or on its innovations. Between #8 and #9: the estimator shape is a
second-order effect next to cadence and the family's parameter surface really is
one dimension, in which case the honest next trial is a robustness check on #8
rather than another variant. Below #8: the extra flips the decay admits are
whipsaw — decay is recency without persistence, and the finding would be that
CVD's predictive content here is in the *slow* component, which is worth knowing
before any faster estimator is tried.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation: hold with the flow, flip on a decisive reversal.

    The flow statistic is a time-decayed (geometric) cumulative volume delta
    rather than a rectangular-window one; everything else is #8.
    """

    def __init__(self, interval="15m", half_life_bars=48, sd_window=2880,
                 min_sd_obs=672, entry_z=0.5, flip_z=1.0, size=1,
                 stop_bps=1500.0, cooldown_bars=96, min_flow_bars=8):
        self.interval = interval            # 15m bars: #8's clock
        self.half_life_bars = max(1.0, float(half_life_bars))  # 48 bars = 12 hours
        self.sd_window = sd_window          # 2880 bars = 30 days of ratio history
        self.min_sd_obs = min_sd_obs        # 672 bars = 7 days before a scale exists
        self.entry_z = entry_z              # |z| needed to open from flat
        self.flip_z = flip_z                # |z| needed to reverse an open position
        self.size = size
        self.stop_bps = stop_bps            # catastrophe stop, basis points
        self.cooldown_bars = cooldown_bars  # 96 bars = 24 hours flat after a stop
        self.min_flow_bars = min_flow_bars  # bars before any ratio exists at all

        # Decayed flow sums: num = sum(w * delta), den = sum(w * volume).
        # Two floats, no history, no exit discontinuity.
        self.decay = math.exp(-math.log(2.0) / self.half_life_bars)
        self.num = 0.0
        self.den = 0.0
        self.flow_bars = 0

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

        # --- decayed flow, O(1) ---------------------------------------------
        self.num = self.decay * self.num + float(bar.delta)
        self.den = self.decay * self.den + float(bar.volume)
        self.flow_bars += 1

        # A handful of bars already forms a reading of the same statistic;
        # refusing to form one is an estimator warm-up, not a market state.
        if self.flow_bars < self.min_flow_bars or self.den <= 0.0:
            return
        ratio = self.num / self.den

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
            # what is missing is only the yardstick for "large". With no yardstick
            # the sign of the flow is the whole of the evidence, so read it as
            # maximal in that direction rather than declining to act.
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
        # Keep the direction in step with what actually happened; a flip is a
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
