"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices, and aggressive selling by lower ones. The sign of the
accumulated flow is the sign of the position. This file follows the flow; it does
not fade it — a fade would be a test of the opposite claim and this campaign
named the direction.

Where sixteen trials have landed
--------------------------------
#0 (1h bars, 6h flow, 6h hold, rolling quantile) made 1,196 fills and scored
-0.1415: at 10 bp a round trip the fee bill was the whole of the loss, so its
gross edge was about zero. #1 (short hold, wide bracket) was genuinely negative
gross. #2/#3 cut the trade count and were refused for the exposure floor (251 and
392 of 960 scored sessions against a required 70%). #4 — a persistent position
held on the sign of a 48-hour **boxcar** flow ratio, z-scored against its own
trailing 30-day dispersion, entry |z| > 0.5, flip |z| > 1.0, evaluated hourly —
traded 161 fills for **+0.1365**. #5 (72h window) +0.0201 with 133 fills. #6
(12/24/48 consensus) +0.0864 with 264. #7 (1h, flip 1.25 **and** a 12-bar minimum
hold — two changes at once) +0.0778 with 127. #8 — **#4 evaluated on 15-minute
bars, bands untouched — +0.1677 with 167 fills**, still the best of fifteen. #9
(1h, flip 0.75) +0.1545 with 221. #10/#11 (15m, flip 0.75) +0.1269 with 252. #12
(15m, 48-bar-half-life geometric flow) +0.0498 with 310. #13 (15m, 64-bar
half-life, mean age held at #8's 96 bars) +0.0898 with 227. #14 (5m clock, every
band and window held at the same number of hours) **+0.1412 with 207 fills**. #15
(5m clock with the partial-window gate and a tighter flow-warm-up) +0.1381 with
220.

Three axes are closed and I will not spend another trial on them
----------------------------------------------------------------
* **The shape.** A continuously held long-or-short clears the 70% exposure floor
  by construction, and a flip that takes +n straight to -n in a single order
  spends no bar flat and carries no half-filled state. #2/#3 failed the floor by
  tuning a trade rate; nothing here does.
* **The estimator's memory and shape.** Both smoothings tried — 12h and 16h
  half-lives — admit 40-85% more fills and score materially worse. The predictive
  content of CVD here lives in the slow component, and a hard window that
  genuinely forgets a two-day-old burst is doing real work. #5 (72h) shows the
  other side of the same optimum is worse too.
* **The band.** 0.75 / 1.0 / 1.25 across two cadences span +0.078 to +0.168 with
  no monotone structure once read as a 2x2; the ordering reverses between 1h and
  15m, which is what sampling error over ~200 flips looks like.

What #14 and #15 actually taught, and why it is the last piece of information
----------------------------------------------------------------------------
The cadence lever had a measured slope: 1h -> 15m moved +0.1365 -> +0.1677 while
adding only six fills. #14 took the next step — the same 48-hour boxcar, the same
bands, the same state machine, evaluated on 5-minute bars — and **went the other
way**: +0.1412 with **207** fills. #15, with a tighter warm-up gate, repeated it:
+0.1381 with 220.

The fill count is the whole of that result and it is not ambiguous. Going from a
15-minute sampler to a 5-minute sampler added 40-53 flips, and those marginal
flips are worth about 5-6 bp of *negative* net each — they were paid for and not
earned. They cannot be crossings the 15-minute clock simply never reached: a
persistent crossing is still there at the quarter hour and the coarse sampler
would have taken it too, just later. The only crossings that a 5-minute sampler
sees and a 15-minute sampler does not are the ones that **appear and reverse
inside a quarter of an hour**. Those are exactly the crossings that carry no
continuation, and they are exactly what the finer clock bought.

So the coarse cadence was not merely a lag; it was, accidentally, a **filter**.
#8's advantage over #14 is not that it responded at the right moment — #8 is the
*slower* of the two and the record is unambiguous that slowness is costly, since
1h < 15m. It is that a clock boundary silently discards every excursion that
does not survive to the boundary.

That gives one more experiment in this family, and it is the only one left whose
sign is predicted by the data rather than by a story: **separate the filter from
the clock.** Keep the 5-minute observation of the flow — the resolution the
signal deserves, and the resolution that removes up to ten minutes of avoidable
staleness per flip — and replace the accidental filter (wait for a clock edge)
with an explicit one (the crossing must persist).

The single change
-----------------
Everything in #8 is kept: the 48-hour boxcar, the ratio
`sum(delta)/sum(volume)`, the 30-day dispersion scale, `entry_z = 0.5`,
`flip_z = 1.0`, market orders, one lot, the 15% catastrophe stop, the 24-hour
cooldown, and every parameter expressed in the same number of hours:

    interval      "5m"     288 bars a day, so a crossing is *seen* promptly
    flow_window   576 bars  = 48 hours
    sd_window    8640 bars  = 30 days
    min_sd_obs   2016 bars  =  7 days
    cooldown      288 bars  = 24 hours

and one new parameter:

    confirm_bars  3         = 15 minutes of persistence before a crossing is
                            traded

A decision now requires the threshold to hold on `confirm_bars` **consecutive
closed 5-minute bars**:

* opening from flat needs `z > entry_z` (or `< -entry_z`) for three bars running;
* reversing needs `z < -flip_z` (or `> flip_z`) for three bars running.

A crossing that reverses within the window resets its counter and is never
traded. A crossing that persists is acted on at most fifteen minutes after it
begins — and, unlike #8, with no dependence on where the crossing fell relative
to an arbitrary clock edge. Average staleness is comparable to #8's 7.5 minutes;
the *worst case* is fifteen minutes instead of fifteen, but the *distribution*
is tight instead of uniform, and the excursions #14 showed to be worthless are
gone by construction rather than by luck.

This is a clean 2x2 against what already exists: {15m, 5m} x {clock filter,
persistence filter}. #8 is (15m, clock), #14 is (5m, none), and this file is
(5m, persistence). Whatever it scores, the fourth cell is interpretable — if
this beats #8, the filter is real and the 15-minute clock was costing part of
what it saved; if it lands at #8, the crossing-persistence horizon and the clock
horizon are the same thing seen two ways, which is itself worth knowing; if it
comes in with ~200 fills it means the transients live *longer* than fifteen
minutes, and the whole cadence-and-filter axis is closed for good.

The honest prediction
---------------------
Fill count a little **below** #8's 167: the transients #14 admitted are removed,
and almost nothing else changes, since a crossing that survives fifteen minutes
was going to be acted on by #8 as well. Score near #8, with the possibility of a
small gain from responding within the bar rather than at the boundary. I am not
promising more than that — the measured size of the cadence effect between 1h and
15m was 1.9 bp per flip, and this recovers at most a fraction of a 15-minute
window's worth.

The rest is #8, unchanged, and deliberately so
---------------------------------------------
* **Measurement.** `ratio = sum(bar.delta) / sum(bar.volume)` is cumulative
  volume delta made dimensionless — the fraction of the window's aggressive
  volume that was net buying, minus net selling. Dividing by the window's own
  volume is the campaign's "how to measure large" answered once: 500 BTC of net
  buying is a different event in a quiet Asian morning and at a US open.
  `unknown_volume` is zero on this feed — every trade carries its aggressor, and
  the run facts say so — so `bar.delta` is the exact signed flow, there is no
  unknown branch to decide, and no tick subscription is needed: ticks cost
  roughly five times the wall clock for a number the bar already carries exactly.

* **"Large" is standardised.** `z = ratio / sd(ratio)` over the trailing 30 days
  of ratio observations, needing 7 days before a scale exists. A fixed cut-off on
  a 24/7 asset is a bet on the volatility regime of the year it was chosen in.
  The cut is on the **level** of the ratio, not its deviation from a trailing
  mean: the claim is that one-sided flow predicts, and a month of persistent net
  buying must not be relabelled "not unusual" merely because it happens to be the
  local average.

* **Hysteresis between entry and flip.** Opening from flat needs |z| > 0.5;
  reversing needs |z| > 1.0. A flip pays two fees and throws away a position the
  flow has not yet contradicted, so it demands a more decisive reversal. The gap
  is what keeps the position on almost always without ever paying to sit out,
  which is also what clears the exposure floor by construction.

* **Market orders, and only on flips.** The fill model is book-queue-pessimistic:
  a resting limit joins behind the displayed size and fills only when trades
  reach through it, which on a continuation entry selects precisely the cases
  where the expected move did not arrive. Paying the touch for a fill that
  actually happens is the cheaper error, and 100 ms of latency against a
  five-minute decision interval is a rounding error on the effect under test.

* **Risk is a backstop, not a rule.** A close-of-bar check at 15% of adverse
  excursion from the average entry, held in code rather than armed as an engine
  order, so no bracket validation, DAY expiry or OCO event can end the run and no
  order id is carried across a callback where an expiry could make it stale. It
  is meant to be crossed by a crash and not by ordinary noise; afterwards the
  strategy stands aside for a day and re-enters on a fresh reading.

* **Neither estimator gate can silently make the run idle.** A ratio forms once
  the flow window is full, and once a ratio exists but the dispersion sample is
  still short, an undefined `z` is read as maximal in the sign of `ratio` rather
  than as a reason not to act. An absent yardstick for "large" is an estimator
  warm-up, not a market state. The confirmation counter is satisfied by that
  fallback in three bars, so a short warmup cannot leave the run flat and
  under-exposed.

* **Bookkeeping is O(1) per bar and bounded.** The flow window is a 576-element
  deque with running delta and volume sums; the dispersion is running sums over a
  bounded 8,640-element deque with a periodic exact re-sync; the confirmation is
  four integer counters. Nothing rescans history — there is no `ctx.history` read
  at all, so the 512-bar pushed window cannot be overrun — and nothing grows
  across sessions. Roughly 276,000 bar callbacks over the split, each a constant
  number of float operations.

Position size is one lot, unchanged. Fees, funding and P&L are all linear in size
and the score is a return on notional, so the result is scale-free and the
smallest tradable size keeps the trial's risk honest while the statistics
accumulate. Scaling up would buy nothing except a bigger number in whatever units
the score happens to be expressed in, and would make this trial incomparable with
the fifteen before it.

What is deliberately absent: a tick subscription, any fade (the campaign names the
direction), any book-based input (not available on this adapter), a regime model
(the standardisation is a scale, not a state), a second instrument, a target or
bracket (a different claim — where the move stops), any fixed-horizon exit (the
horizon *is* the flow window and the position is held while the flow agrees), and
any change to the window, the bands or the state machine, all of which the record
has already closed.

Outcomes and what they would mean
---------------------------------
Above #8 (+0.1677) with a fill count a little under 167: the 15-minute clock was
paying for its filtering with lag, the persistence filter keeps the filtering and
returns most of the lag, and one further trial would tighten `confirm_bars`
towards the shortest persistence that still rejects #14's transients. Within a
few hundredths of #8: the two filters are the same filter, the cadence axis is
closed, and the campaign's answer is the one #8 gave — aggressive flow continues,
worth about 20 bp gross and 10 bp net per flip, and no more. A fill count back up
near 200 with a score below #8: transients survive longer than fifteen minutes, a
persistence filter cannot separate them from signal at this resolution, and the
family is complete.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation: hold with the flow, flip only on a reversal that lasts.

    #8's statistic, #8's bands and #8's state machine on a 5-minute clock, with
    the clock's accidental filter made explicit: a threshold crossing is traded
    only after it has held for `confirm_bars` consecutive closed bars.
    """

    def __init__(self, interval="5m", flow_window=576, sd_window=8640,
                 min_sd_obs=2016, entry_z=0.5, flip_z=1.0, confirm_bars=3,
                 size=1, stop_bps=1500.0, cooldown_bars=288):
        self.interval = interval            # 5m: see the flow promptly
        self.flow_window = flow_window      # 576 bars = 48 hours of aggressor flow
        self.sd_window = sd_window          # 8640 bars = 30 days of ratio history
        self.min_sd_obs = min_sd_obs        # 2016 bars = 7 days before a scale exists
        self.entry_z = entry_z              # |z| needed to open from flat
        self.flip_z = flip_z                # |z| needed to reverse an open position
        self.confirm_bars = confirm_bars    # 3 bars = 15 min of persistence required
        self.size = size
        self.stop_bps = stop_bps            # catastrophe stop, basis points
        self.cooldown_bars = cooldown_bars  # 288 bars = 24 hours flat after a stop

        # Rolling flow window with running sums: (delta, volume) per closed bar.
        self.flow = deque()
        self.sum_delta = 0.0
        self.sum_volume = 0.0

        # Rolling distribution of the raw ratio: running sums, bounded deque.
        self.ratios = deque()
        self.sr = 0.0
        self.sr2 = 0.0
        self._bars = 0

        # Persistence counters: consecutive closed bars a condition has held.
        self.weak_pos = 0                   # z >  entry_z
        self.weak_neg = 0                   # z < -entry_z
        self.strong_pos = 0                 # z >  flip_z
        self.strong_neg = 0                 # z < -flip_z

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
        # Summed over the same 48 hours in 5-minute pieces this is the identical
        # number #8 computed from 15-minute pieces; only the sampling changed.
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
                    self.cooldown = self.cooldown_bars
                    self._reset_streaks()
                    return

        z = self._z(ratio)
        if z is None:
            # No dispersion estimate yet. `ratio` is a number the data supplied;
            # what is missing is only the yardstick for "large". With no yardstick
            # the sign of the flow is the whole of the evidence, so read it as
            # maximal in that direction rather than declining to act -- an
            # undefined scale is an estimator warm-up, not a market state.
            if ratio == 0.0:
                self._reset_streaks()
                return
            z = 2.0 if ratio > 0.0 else -2.0

        # ---- persistence, made explicit -------------------------------------
        # This is the whole experiment: a crossing is not a decision until it has
        # survived `confirm_bars` closed bars. #8 got the same suppression for
        # free from its clock edge, at the price of a uniform 0-15 minute wait;
        # #14 removed the wait and paid for it with transients. Here the filter
        # is stated rather than implied, and the wait is short and bounded.
        if z > self.flip_z:
            self.strong_pos += 1
        else:
            self.strong_pos = 0
        if z < -self.flip_z:
            self.strong_neg += 1
        else:
            self.strong_neg = 0
        if z > self.entry_z:
            self.weak_pos += 1
        else:
            self.weak_pos = 0
        if z < -self.entry_z:
            self.weak_neg += 1
        else:
            self.weak_neg = 0

        need = self.confirm_bars

        # ---- already positioned: hold unless the flow has reversed hard ------
        if position > 0:
            if self.strong_neg >= need:
                # One order takes the book from +position to -size: no bar spent
                # flat, and no half-applied flip if only the close had filled.
                if ctx.sell(size=position + self.size,
                            tag="cvd-flip-short") is not None:
                    self._reset_streaks()
            return
        if position < 0:
            if self.strong_pos >= need:
                if ctx.buy(size=(-position) + self.size,
                           tag="cvd-flip-long") is not None:
                    self._reset_streaks()
            return

        # ---- flat: open with the sign of the flow ----------------------------
        if self.cooldown > 0:
            return
        if self.weak_pos >= need:
            if ctx.buy(size=self.size, tag="cvd-long") is not None:
                self._reset_streaks()
        elif self.weak_neg >= need:
            if ctx.sell(size=self.size, tag="cvd-short") is not None:
                self._reset_streaks()

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx):
        # Nothing to reconcile from the fill itself: the direction is re-read
        # from ctx.position on every bar, and a flip is a single order, so no
        # order id is held across callbacks for an expiry to make stale.
        pass

    def on_lifecycle(self, ctx):
        pass

    # ---------------------------------------------------------------- helpers

    def _reset_streaks(self):
        self.weak_pos = 0
        self.weak_neg = 0
        self.strong_pos = 0
        self.strong_neg = 0

    def _push_ratio(self, value):
        if len(self.ratios) >= self.sd_window:
            old = self.ratios.popleft()
            self.sr -= old
            self.sr2 -= old * old
        self.ratios.append(value)
        self.sr += value
        self.sr2 += value * value

    def _resync(self):
        # Rolling sums of squares drift after tens of thousands of updates;
        # re-deriving them from the bounded deques is cheap and exact.
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
