"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices, and aggressive selling by lower ones. The sign of the
accumulated flow is the sign of the position. This file follows the flow; it does
not fade it — a fade would test the opposite claim and this campaign named the
direction.

Where eighteen trials have landed
---------------------------------
#0 (1h, 6h flow, 6h hold) 1,196 fills, -0.1415: at 10 bp a round trip its fee bill
was ~60% of notional and its gross was ~0. #1 negative gross at a short horizon.
#2/#3 cut the trade count and were refused for the exposure floor (251 and 392 of
960 scored sessions against a required 70%). #4 — a persistent position on the
sign of a 48-hour boxcar flow ratio, z-scored against its own trailing 30-day
dispersion, entry |z| > 0.5, flip |z| > 1.0, hourly — +0.1365 with 161 fills. #5
(72h window) +0.0201. #6 (12/24/48 consensus) +0.0864. #7 (flip 1.25 plus a
12-bar minimum hold) +0.0778. #8 — **#4 evaluated on 15-minute bars, bands
untouched — +0.1677 with 167 fills**, still the best. #9 (1h, flip 0.75) +0.1545.
#10/#11 (15m, flip 0.75) +0.1269 with 252. #12 (15m, 48-bar geometric flow)
+0.0498 with 310. #13 (15m, 64-bar kernel) +0.0898 with 227. #14/#15 (5m clock,
every band held in hours) +0.1412 and +0.1381 with 207 and 220. #16 (15m, flip
vetoed unless the last 6h of flow agree) +0.1513 with 169. #17 (5m clock, a
crossing must persist 3 closed bars) +0.1577 with 169.

What that record has closed, and I will not spend another trial on it
--------------------------------------------------------------------
* **The shape.** A continuously held long-or-short clears the 70% exposure floor
  by construction; a flip that takes +n straight to -n in one order spends no bar
  flat and carries no half-filled state. #2/#3 failed the floor by tuning a trade
  rate. Nothing since has.
* **The horizon.** 6h gross-negative (#0/#1), 48h best (#4/#8), 72h far worse
  (#5). Closed from both ends.
* **The cadence.** 1h +0.1365, 15m +0.1677, 5m +0.1412, 5m+persistence +0.1577,
  15m+freshness-veto +0.1513. The peak is 15 minutes and every attempt to beat it
  by moving the clock, or by replacing the clock's accidental filter with an
  explicit one, landed one to three hundredths below it. #16 and #17 both held the
  fill count near #8's and lost 1.5-2 bp: the crossings they delayed were worth
  more than the fees they saved.
* **The estimator's *time* weighting.** Two smoothings (#12, #13) raised the fill
  count by 36-85% and scored materially worse. Recency as a weight on the signal
  is dead.
* **The band.** 0.75 / 1.0 / 1.25 across two cadences span +0.078 to +0.168 with
  the ordering reversing between 1h and 15m — sampling error over ~200 flips.

So the family is at a measured optimum on cadence, window, band, smoothing and
state machine, and the remaining spread of +0.02 to +0.17 across configurations
that differ only in those parameters is the same size as the per-trial sampling
error. Anything left has to attack a part of the estimator that none of the
eighteen trials has touched.

What is still untouched: how much one bar counts *inside* the window
--------------------------------------------------------------------
The statistic is

    ratio = sum(bar.delta) / sum(bar.volume)

and every trial so far has been volume-weighted: a bar that carried ten times the
window's typical volume contributed ten times as much to the numerator and ten
times as much to the denominator. The record has varied the window over which
that sum runs, the clock at which it is sampled, the shape of the decay applied
to it, and the bands it must cross. It has never varied the weight a single bar
carries inside the sum.

That weight is not a detail on this asset. One 15-minute bar in a cascade can
carry several percent of a 48-hour window's volume and be almost entirely
one-sided; at p = 1 such a bar moves `ratio` by an amount comparable to the whole
window's standard deviation, which is to say by more than a band. So a single
print can, by itself, flip the position.

The campaign names the reason this matters before any of us ran a strategy:
"Crypto flow can also be dominated by liquidations, which may exhaust
themselves quickly." A liquidation cascade is exactly that bar — enormous volume,
extreme one-sided delta, a price move that continues for minutes and then mean
reverts. The current statistic reads it as the strongest possible evidence of
continuation and flips *into* the top of it, paying 10 bp for the privilege. If
any part of the distance between #8's +0.25 gross and its +0.17 net is that, the
loss lives in a specific, identifiable set of bars rather than in the timing of
ordinary crossings — which is the opposite of what #16 and #17 tested, and the
opposite of what they found.

The single change: dampen a bar's weight by the square root of its volume
-----------------------------------------------------------------------
Weight each bar by `volume ** p` with **p = 0.5** instead of p = 1:

    ratio = sum(w_i * f_i) / sum(w_i),   f_i = delta_i / volume_i,   w_i = volume_i ** p

At p = 1 this is `sum(delta)/sum(volume)` exactly — #8, unchanged, and the
default reproduces it if the exponent is set back. At p = 0 every bar votes once
regardless of size. p = 0.5 is the midpoint, and its concrete effect is that a
bar with ten times the typical volume carries 3.2 times the weight instead of
ten. Large prints still dominate; they no longer *are* the statistic.

Nothing else changes. The 15-minute clock, the 48-hour window, the 30-day
dispersion scale, the 7-day warm-up, `entry_z = 0.5`, `flip_z = 1.0`, market
orders on flips only, one lot, the 15% catastrophe stop and the 24-hour cooldown
are all #8's. The bands stay meaningful across the change for a structural
reason: `z = ratio / sd(ratio)` is taken against the trailing distribution of the
*same* statistic, so a change of units in `ratio` is absorbed by its denominator
and |z| > 1 still means "one standard deviation of what this thing usually does".

Why this and not something else
-------------------------------
* **It is the one piece of the estimator that is still free.** Window, cadence,
  decay shape and band are each closed by direct measurement; the within-window
  weighting has never been varied. Four of the last five trials moved along axes
  the record had already bracketed and each landed within 2 bp of #8. This one
  changes the composition of the signal, not its timing.
* **It targets the mechanism the campaign itself flagged.** Volume-weighted CVD
  is a sum over prints, so it is maximally exposed to the one kind of print the
  campaign warns about. If continuation fails because of liquidations, dampening
  them is where the improvement is; if continuation is genuine impact, dampening
  them costs a little and the score says so.
* **It has a falsifiable sign, in both directions.** Fewer whipsaw flips and a
  higher average flip quality means p = 0.5 wins outright. A materially lower
  score means the largest prints carry the signal — impact is real and
  proportional to size — which is a clean confirmation of the claim's own
  mechanism and closes the weighting axis at the p = 1 end.
* **The change is small and the risk is bounded.** Adjacent bars differ by a
  factor of a few in volume, so for most of the window the weighting is barely
  different from p = 1; the statistic only departs from #8's where a bar is
  large. The fill count should move by a few percent, and the run cannot crash:
  the same O(1) running sums, the same bounded deques, no `ctx.history` read.

The rest is #8, unchanged, and deliberately so
---------------------------------------------
* **Measurement.** `delta` is the exact signed aggressor flow. `unknown_volume`
  is zero on this feed — every trade carries its aggressor and the run facts say
  so — so there is no unknown branch to decide and no tick subscription is
  needed: ticks cost roughly five times the wall clock for a number the bar
  already carries exactly.

* **"Large" is standardised.** The cut is on the *level* of the ratio, not its
  deviation from a trailing mean: the claim is that one-sided flow predicts, and
  a month of persistent net buying must not be relabelled "not unusual" merely
  because it is the local average.

* **Hysteresis between entry and flip.** Opening from flat needs |z| > 0.5;
  reversing needs |z| > 1.0. A flip pays two fees and throws away a position the
  flow has not yet contradicted, so it demands a more decisive reversal. The gap
  is what keeps the position on almost always without ever paying to sit out.

* **Market orders, and only on flips.** The fill model is book-queue-pessimistic:
  a resting limit joins behind the displayed size and fills only when trades reach
  through it, which on a continuation entry selects precisely the cases where the
  expected move did not arrive — and for a momentum flip it would fill only after
  the new direction had already paid. Paying the touch for a fill that happens is
  the cheaper error, and 100 ms against a 15-minute decision interval is a
  rounding error.

* **Risk is a backstop, not a rule.** A close-of-bar check at 15% of adverse
  excursion from the average entry, held in code rather than armed as an engine
  order, so no bracket validation, DAY expiry or OCO event can end the run and no
  order id is carried across a callback where an expiry could make it stale. It
  is meant to be crossed by a crash, not by ordinary noise; afterwards the
  strategy stands aside for a day and re-enters on a fresh reading.

* **No estimator gate can silently make the run idle.** A ratio forms once the
  flow window is full; once a ratio exists but the dispersion sample is still
  short, an undefined `z` is read as maximal in the sign of `ratio` rather than
  as a reason not to act. An absent yardstick for "large" is an estimator
  warm-up, not a market state.

* **Bookkeeping is O(1) per bar and bounded.** One 192-element deque with running
  numerator and denominator sums, running dispersion over a bounded 2,880-element
  deque with a periodic exact re-sync, and a cooldown counter. There is no
  `ctx.history` read at all, so the 512-bar pushed window cannot be overrun, and
  nothing grows across sessions.

Position size is one lot, unchanged. The score is a return on notional, so it is
invariant to size; keeping it identical is also the only reason a difference of a
few hundredths can be read against the eighteen trials before it.

What is deliberately absent: a tick subscription, any fade, any book-based input,
a regime model, a second instrument, a target or bracket, a fixed-horizon exit,
and any change to the window, the bands, the cadence or the state machine, all of
which the record has already closed.

One thing I want on the record about the whole family
-----------------------------------------------------
A 48-hour flow ratio gives roughly 83 flips over 960 sessions, an average hold of
11.5 days, and a gross edge per flip near 30 bp against an 11.5-day price move
with a standard deviation near 8%. The per-trial t-statistic of the family's edge
is around 0.3. I am running this trial because it is a mechanism with a measured
target — the campaign's own liquidation warning, made concrete in a parameter that
has never been varied — not because I believe +0.1677 is signal.

Outcomes and what they would mean
---------------------------------
**Above #8 with a similar fill count:** large prints were part of the problem, the
estimator's weighting axis is live, and the next trial is p = 0.25 or p = 0 on the
same skeleton — same window, same bands, same cadence — to find where the
dampening stops paying. **Within a few hundredths of #8:** volume weighting is not
where the loss is, the estimator axis is closed at the p = 1 end, and the
campaign's answer is the one #8 already gave — aggressive flow continues, worth
about 20 bp gross and 10 bp net per flip, and no more. **Materially below #8, or a
fill count that jumps:** dampening the largest bars removes real signal, the
impact story holds at bar resolution, and the volume-weighted sum was right all
along.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation: hold with the flow, flip only on a decisive reversal.

    #8's statistic, bands, cadence and state machine, with one change: a bar's
    weight inside the flow window is `volume ** volume_exponent` rather than
    `volume`. At `volume_exponent = 1.0` this is exactly #8.
    """

    def __init__(self, interval="15m", flow_window=192, sd_window=2880,
                 min_sd_obs=672, entry_z=0.5, flip_z=1.0, size=1,
                 stop_bps=1500.0, cooldown_bars=96, volume_exponent=0.5):
        self.interval = interval                    # 15m: the measured cadence peak
        self.flow_window = flow_window              # 192 bars = 48 hours
        self.sd_window = sd_window                  # 2880 bars = 30 days of ratios
        self.min_sd_obs = min_sd_obs                # 672 bars = 7 days before a scale
        self.entry_z = entry_z                      # |z| needed to open from flat
        self.flip_z = flip_z                        # |z| needed to reverse a position
        self.size = size
        self.stop_bps = stop_bps                    # catastrophe stop, basis points
        self.cooldown_bars = cooldown_bars          # 96 bars = 24 hours after a stop
        self.p = float(volume_exponent)             # 1.0 = #8, 0.5 = this trial

        # Flow window: per bar we keep (w * imbalance, w) so that the ratio is
        # sum(num) / sum(den) and a bar can be dropped in O(1).
        self.flow = deque()
        self.sum_num = 0.0
        self.sum_den = 0.0

        # Rolling distribution of the raw ratio: running sums, bounded deque.
        self.ratios = deque()
        self.sr = 0.0
        self.sr2 = 0.0
        self._bars = 0

        self.dir = 0
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
        # w = volume ** p and the numerator carries w * (delta / volume) =
        # delta * volume ** (p - 1). At p = 1 both reduce to #8 exactly.
        delta = float(bar.delta)
        volume = float(bar.volume)
        if volume > 0.0:
            weight = volume ** self.p
            num = delta * (volume ** (self.p - 1.0))
        else:
            weight = 0.0
            num = 0.0

        if len(self.flow) >= self.flow_window:
            old_num, old_weight = self.flow.popleft()
            self.sum_num -= old_num
            self.sum_den -= old_weight
        self.flow.append((num, weight))
        self.sum_num += num
        self.sum_den += weight

        if len(self.flow) < self.flow_window or self.sum_den <= 0.0:
            return
        ratio = self.sum_num / self.sum_den

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
            # what is missing is only the yardstick for "large", so read the sign
            # of the flow as maximal rather than declining to act.
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
        # Nothing to reconcile: the direction is re-read from ctx.position on
        # every bar, and a flip is a single order, so no order id is held across
        # callbacks for an expiry to make stale.
        pass

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
        # Rolling sums drift after tens of thousands of updates; re-deriving them
        # from the bounded deques is cheap and exact.
        self.sr = 0.0
        self.sr2 = 0.0
        for value in self.ratios:
            self.sr += value
            self.sr2 += value * value
        num = 0.0
        den = 0.0
        for bar_num, bar_weight in self.flow:
            num += bar_num
            den += bar_weight
        self.sum_num = num
        self.sum_den = den

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
