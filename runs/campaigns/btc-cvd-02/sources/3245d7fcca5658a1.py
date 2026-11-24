"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices, and aggressive selling by lower ones. The sign of the
accumulated flow is the sign of the position. This file follows the flow; it does
not fade it — a fade would be a test of the opposite claim and this campaign
named the direction.

The record, and what it has settled
-----------------------------------
#0 (1h bars, 6h flow, 6h hold, rolling quantile) made 1,196 fills and scored
-0.1415: at 10 bp a round trip the fee bill was the whole of the loss, so its
gross edge was about zero. #1 (short hold, wide bracket) was genuinely negative
gross. #2/#3 cut the trade count and were refused for the exposure floor. #4 — a
persistent position held on the sign of a 48-hour **boxcar** flow ratio, z-scored
against its own trailing 30-day dispersion, entry |z| > 0.5, flip |z| > 1.0,
evaluated hourly — traded 161 fills for **+0.1365**. #5 (72h window) +0.0201. #6
(12/24/48 consensus) +0.0864. #7 (flip 1.25, 12-bar minimum hold) +0.0778. #8 —
**#4 evaluated on 15-minute bars, bands untouched — +0.1677 with 167 fills**, the
best of the fourteen. #9 (1h, flip 0.75) +0.1545; #10/#11 (15m, flip 0.75)
+0.1269; #12 (15m, 48-bar-half-life geometric flow) +0.0498 with 310 fills; #13
(15m, 64-bar-half-life exponential kernel) +0.0898 with 227 fills.

Three things are settled and I will not spend another trial on them:

* **The shape.** A continuously held long-or-short clears the 70% exposure floor
  by construction, and a flip that takes +n straight to -n in a single order
  spends no bar flat and carries no half-filled state. #2/#3 failed the floor by
  tuning a trade rate; nothing here does.
* **The estimator's memory is a hard 48 hours and its shape is a boxcar.** Both
  smoothings tried — 12h and 16h half-lives — admit roughly 40-85% more fills
  and score materially worse. Time-decay is recency without persistence: the
  extra crossings it admits are whipsaw, not signal. The predictive content of
  CVD here lives in the slow component, and a hard window that genuinely forgets
  a two-day-old burst is doing real work. #5 (72h) shows the other side of the
  same optimum is also worse.
* **The band is flat.** 0.75 / 1.0 / 1.25 across two cadences span +0.078 to
  +0.168 with no monotone structure once the 2x2 is read together — the ordering
  reverses between 1h and 15m, which is what sampling error over ~200 flips looks
  like. Four configs that differ only in the band average +0.146 with a spread of
  0.041. That spread is not a signal, and moving along it again would be
  measuring noise and calling it a parameter.

What is left is the one lever whose direction was confirmed, and it has not been
exhausted
--------------------------------------------------------------------------
Cadence. At constant bands and constant window, 1h -> 15m moved +0.1365 ->
+0.1677 while adding only six fills. Those six fills cannot be worth 0.031 net
(that would be 52 bp each, against a 10 bp round trip and a 20 bp gross edge), so
the gain is not the extra trades: it is that the **same 161 flips reached the
market sooner**. Up to 37 minutes of staleness was removed from each one, worth
about 1.9 bp per flip.

That is a lever with a measured slope and a floor it has not reached. The average
staleness at 15m is still 7.5 minutes; at 5m it is 2.5. So this file keeps every
one of #8's choices — the 48-hour boxcar, the 30-day dispersion scale, entry 0.5,
flip 1.0, market orders on flips only, one lot, the 15% catastrophe stop, the
24-hour cooldown — and evaluates it on **5-minute bars**.

Why this is close to a pure timing change, and not a new signal
--------------------------------------------------------------
The statistic is `sum(bar.delta) / sum(bar.volume)` over a fixed span of wall
time. Summing the same 48 hours in 5-minute pieces instead of 15-minute pieces
gives **the identical number** — addition does not care how the interval is cut,
and bars never straddle a session boundary. What changes is only *when* the sum
is looked at: 288 times a session instead of 96. So the sequence of threshold
crossings is the same sequence, shifted earlier by up to ten minutes; the
distribution of `ratio` is unchanged, and `sd(ratio)` over the same 30 days
estimates the same dispersion on a denser sample of the same process. Every
parameter that can be expressed in hours is still expressed in the same number of
hours:

    flow_window   576 bars  = 48 hours   (was 192 at 15m)
    sd_window    8640 bars  = 30 days    (was 2880)
    min_sd_obs   2016 bars  =  7 days    (was 672)
    cooldown      288 bars  = 24 hours   (was 96)

and 576 + 2016 = 2592 bars = 9 sessions, exactly the depth of run-time history
#8 needed before it could act — so this file cannot be gated out by a warmup that
was sufficient for the best-scoring trial.

The honest risk, stated in advance
----------------------------------
At 5 minutes a crossing that appears and reverses inside a quarter of an hour is
now visible and will be traded; at 15 minutes it was invisible. That is the only
way this file can add a fill, and each such fill costs 10 bp for nothing. It
should be rare — `ratio` is a 48-hour aggregate, so a single 5-minute bar moves it
by roughly one part in 576 of that bar's imbalance, and a genuine crossing
persists for many bars. If the fill count comes in materially above 167, that
assumption was wrong and the result will say so.

The expected size of the improvement is small and I will say so plainly: at the
measured 0.084 bp of edge per minute of staleness removed, cutting average
staleness from 7.5 to 2.5 minutes is worth about +0.007. This trial is a low-
variance extension of a confirmed lever, not a new idea, and if it lands within
noise of #8 the finding is that the cadence lever is exhausted at 15 minutes and
the family has given the campaign its answer.

The rest is #8, unchanged, and deliberately so
---------------------------------------------
* **Measurement.** `ratio = sum(bar.delta) / sum(bar.volume)` is cumulative volume
  delta made dimensionless — the fraction of the window's aggressive volume that
  was net buying, minus net selling. Dividing by the window's own volume is the
  campaign's "how to measure large" answered once: 500 BTC of net buying is a
  different event in a quiet Asian morning and at a US open. `unknown_volume` is
  zero on this feed — every trade carries its aggressor, and the run facts say so
  — therefore `bar.delta` is the exact signed flow, there is no unknown branch to
  decide, and no tick subscription is needed: ticks cost roughly five times the
  wall clock for a number the bar already carries exactly.

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
  actually happens is the cheaper error, and 100 ms of latency against a 5-minute
  decision interval is a rounding error on the cadence lever itself.

* **Risk is a backstop, not a rule.** A close-of-bar check at 15% of adverse
  excursion from the average entry, held in code rather than armed as an engine
  order, so no bracket validation, DAY expiry or OCO event can end the run and no
  order id is carried across a callback where an expiry could make it stale. It
  is meant to be crossed by a crash and not by ordinary noise; afterwards the
  strategy stands aside for a day and re-enters on a fresh reading.

* **Neither estimator gate can silently make the run idle.** A reading forms on
  the statistic's own terms once the flow window is full, and once a ratio exists
  but the dispersion sample is still short, an undefined `z` is read as maximal in
  the sign of `ratio` rather than as a reason not to act. An absent yardstick for
  "large" is an estimator warm-up, not a market state, and gating on one silently
  is how an earlier draft of this family made no trades at all on a short walk.
  On the real split the 9-session requirement above is long past by the first
  actionable bar, so this branch is inert there.

* **Partial windows never enter the dispersion sample.** The sd deque only
  accepts a ratio once the flow window is genuinely 48 hours old, so the noisy
  short-window readings at the start of the data cannot inflate `sd` for the first
  month of the scored walk and suppress the signal while it matters.

* **Bookkeeping is O(1) per bar and bounded.** The flow window is a 576-element
  deque with running delta and volume sums; the dispersion is running sums over a
  bounded 8,640-element deque with a periodic exact re-sync. Nothing rescans
  history — there is no `ctx.history` read at all, so the 512-bar pushed window
  cannot be overrun — and nothing grows across sessions. Three hundred thousand
  bar callbacks over the split, each a constant number of float operations.

Position size is one lot, unchanged. Fees, funding and P&L are all linear in
size and the score is a return on notional, so the result is scale-free and the
smallest tradable size keeps the trial's risk honest while the statistics
accumulate. Scaling up would buy nothing except a bigger number in whatever units
the score happens to be expressed in, and would make this trial incomparable with
the thirteen before it.

What is deliberately absent: a tick subscription, any fade (the campaign names the
direction), any book-based input (not available on this adapter), a regime model
(the standardisation is a scale, not a state), a second instrument, a target or
bracket (a different claim — where the move stops), any fixed-horizon exit (the
horizon *is* the flow window and the position is held while the flow agrees), and
any change to the window, the bands or the state machine, all of which the record
has already closed.

Outcomes and what they would mean
---------------------------------
Above #8 (+0.1677) with a fill count near 167: the cadence lever still pays at the
margin, and one further step (2m or 1m) is the same experiment once more. Within
noise of #8: the lever is exhausted at 15 minutes, the family's parameter surface
has no direction left in it, and the campaign's answer is the one #8 already gave
— aggressive flow continues, worth about 20 bp gross and 10 bp net per flip, and
no more. Materially below #8, or a fill count well above 167: the extra crossings
the finer clock admits are intra-quarter-hour noise, which would say the
coarse sampling was itself a filter and would close the cadence axis for good.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation: hold with the flow, flip only on a decisive reversal.

    #8's statistic, #8's bands, #8's state machine, evaluated on 5-minute bars:
    the same 48-hour boxcar, looked at three times as often.
    """

    def __init__(self, interval="5m", flow_window=576, sd_window=8640,
                 min_sd_obs=2016, entry_z=0.5, flip_z=1.0, size=1,
                 stop_bps=1500.0, cooldown_bars=288, min_flow_bars=8):
        self.interval = interval            # 5m bars: #8's signal, finer clock
        self.flow_window = flow_window      # 576 bars = 48 hours of aggressor flow
        self.sd_window = sd_window          # 8640 bars = 30 days of ratio history
        self.min_sd_obs = min_sd_obs        # 2016 bars = 7 days before a scale exists
        self.entry_z = entry_z              # |z| needed to open from flat
        self.flip_z = flip_z                # |z| needed to reverse an open position
        self.size = size
        self.stop_bps = stop_bps            # catastrophe stop, basis points
        self.cooldown_bars = cooldown_bars  # 288 bars = 24 hours flat after a stop
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

        if len(self.flow) < self.min_flow_bars or self.sum_volume <= 0.0:
            return
        ratio = self.sum_delta / self.sum_volume

        # A partial window is a noisier reading of the same quantity; let it form
        # a direction if it must, but never let it into the dispersion sample,
        # where it would inflate `sd` for a month of the scored walk.
        window_full = len(self.flow) >= self.flow_window
        if window_full:
            # The scale accumulates through warmup as well: those bars are real,
            # only the orders would not be, and reaching the first actionable bar
            # with no idea what "large" means here would waste the warmup.
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
            # maximal in that direction rather than declining to act -- an
            # undefined scale is an estimator warm-up, not a market state.
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
        # Rolling sums of squares drift after tens of thousands of updates;
        # re-deriving them from the bounded deque is cheap and exact.
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
