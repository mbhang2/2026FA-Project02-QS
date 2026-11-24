"""CVD consensus continuation on the OKX BTCUSDT perpetual — campaign:
"BTC aggressive flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices; aggressive selling by lower ones. The sign of the
position is the sign of the flow. This file does not fade. If continuation is
wrong at this horizon the score says so; rotating to the other side would test a
different hypothesis and answer nothing.

Where the trials have landed
----------------------------
#0 (6h flow, 6h hold, rolling quantile) made 1,196 round trips and scored
-0.1415: at 10 bp a round trip the fee bill is the whole of the loss, so its
gross edge was about zero. #1 (short hold, wide bracket) lost more than fees can
explain, so its gross edge was genuinely negative at the short horizon. #2 and
#3 cut the trade count and were refused for the exposure floor (251 and 392 of
960 scored sessions against a required 70%). #4 — always in the market, one
direction held until the 48-hour flow reverses by more than one standard
deviation — traded 161 times and scored +0.1365, the best result so far. #5, the
same idea with a 72-hour window, a rolling-median band and a 48-bar minimum
hold, traded 133 times and scored +0.0201.

Two things follow from that record. First, the exposure floor is satisfied by
*construction* by being continuously long or short, not by tuning a trade rate;
#4 and #5 both cleared it. Second, within the "hold continuously, flip rarely"
family the construction of the direction estimate moved the score by more than a
tenth of a point, which is far more than the fee difference between 133 and 161
trades. The lever is the *quality of the direction estimate*, not the trade
count. This file leaves #4's state machine alone and attacks the estimator.

What changes here
-----------------
The direction estimate is the quantity #4 used — the fraction of the window's
aggressive volume that was net buying, sum(delta)/sum(volume) — but computed at
three horizons and averaged:

    ratio_k   = sum(delta over last k bars) / sum(volume over last k bars)
    consensus = mean(ratio_12, ratio_24, ratio_48)

This is variance reduction, not a new hypothesis. A single 48-bar ratio is one
noisy draw of "who is winning"; the same statistic over three nested windows is
a smoother estimator of the same state and cannot be arbitrarily different from
it. It amounts to applying a hump-shaped kernel to per-bar delta, and it keeps
the property that makes the ratio usable at all: it is dimensionless, so 500 BTC
of net buying means the same in a quiet Asian morning and at the US open.
`unknown_volume` is zero on this feed — every trade carries its aggressor — so
`bar.delta` is the exact signed flow, there is no unknown branch to decide, and
no tick subscription is needed: ticks cost roughly five times the wall clock for
a number the bar already carries exactly.

The horizons are 12/24/48 rather than 24/48/96 and the scale's warmup is 48
observations rather than 240. That is deliberate, not incidental: a strategy
whose first possible decision is 300+ bars into the walk is a strategy that has
to be trusted on a very short test it cannot trade on at all, and the earlier
week-scale windows also lean further into the slow-drag regime where the flow
edge was weakest in #5. Shorter, nested windows keep the estimator defined early
and, if anything, read the same state with less lag while the trailing dispersion
below still supplies the regime-invariance the campaign asks for.

"Large" is standardised by the trailing 30-day standard deviation of the
consensus itself,

    z = consensus / sd(consensus over the last 720 hourly bars)

so the same threshold means the same thing in every volatility regime; a fixed
cut-off on a 24/7 asset is a bet on the year it was chosen in. The cut is on the
*level*, not on the deviation from a trailing mean: the claim is that one-sided
flow predicts, and a month of persistent net buying should not be relabelled
"not unusual" just because it happens to be the local average. The scale is
allowed to exist after 48 observations rather than 240 — an early sd is noisier,
but the window fills to 720 over a couple of months and the noise is confined to
the first decisions of a 960-session run.

The state machine is #4's, because it is the one that scored. Enter from flat on
|z| > 0.4; reverse only when the flow is a full standard deviation the other
way; hold otherwise, across sessions and across funding times. The one addition
is a minimum holding period of 24 bars — a flip pays two fees and discards a
position the flow has not yet contradicted, so a flip within a day of the last
one is a whipsaw by construction. That is the cost governor stated plainly
rather than a threshold quietly tuned into the band.

Turnover, honestly: at roughly 150 flips over 960 sessions the fee bill is about
1.5% of notional in total, near 4% a year, against a position that is always on.
That is the arithmetic that separates this family from #0: whatever the flow
signal is worth, it is paid for about once a week, not once every six hours.

Risk is a backstop, not a rule: a close-of-bar check at 15% adverse excursion
from the average entry, held in code rather than armed as an engine order, so no
DAY expiry can leave anything naked, no OCO event can end the run, and no order
id is carried across a callback where an expiry could make it stale. After it
fires the strategy stands aside for a day and re-enters on a fresh reading.

Orders are market. The fill model is queue-pessimistic — a resting limit fills
only when trades reach through it — which on a continuation entry selects
exactly the cases where the expected move did not arrive. Paying the touch for a
fill that actually happens is the cheaper error, and 100 ms of latency on a
one-lot order is not meaningful slippage at hourly scale.

Bookkeeping is O(1) per bar: three bounded deques with running sums for the flow
windows, one bounded deque with running sums for the standard deviation,
periodically re-synced exactly. Nothing rescans history, nothing grows without
bound, and there is no `ctx.history` read at all, so the pushed window cannot be
overrun.

Position size is one lot. Fees, funding and P&L are all linear in it, which
keeps the trial's risk honest while the statistics accumulate.

What is deliberately absent: a tick subscription, any fade (the campaign names
the direction), any book-based input (not available on this adapter), a regime
model, a target or bracket (a different claim — where the move stops), a second
instrument, and any fixed-horizon exit — the horizon is the flow window and the
position is held while the flow agrees with it.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation: hold the sign of a multi-horizon flow consensus."""

    def __init__(self, interval="1h", windows=(12, 24, 48), sd_window=720,
                 min_sd_obs=48, entry_z=0.4, flip_z=1.0, min_hold=24,
                 size=1, stop_bps=1500.0, cooldown_bars=24):
        self.interval = interval
        self.windows = tuple(int(k) for k in windows)
        self.sd_window = sd_window            # bars of consensus history
        self.min_sd_obs = min_sd_obs          # observations before a scale exists
        self.entry_z = entry_z                # |z| needed to open from flat
        self.flip_z = flip_z                  # |z| needed to reverse a position
        self.min_hold = min_hold              # bars between flips, at minimum
        self.size = size
        self.stop_bps = stop_bps              # catastrophe stop, basis points
        self.cooldown_bars = cooldown_bars    # flat bars after a stop-out

        # Running sums over each flow window: (delta, volume) per closed bar.
        self._bufs = [deque() for _ in self.windows]
        self._sums = [[0.0, 0.0] for _ in self.windows]

        # Rolling distribution of the consensus: running sums, bounded deque.
        self._cons = deque()
        self._sr = 0.0
        self._sr2 = 0.0
        self._bars = 0

        self.dir = 0                          # last known position direction
        self.cooldown = 0
        self._held = 0                        # actionable bars since last open

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

        delta = float(bar.delta)
        volume = float(bar.volume)

        complete = True
        ratios = []
        for i, k in enumerate(self.windows):
            buf = self._bufs[i]
            sums = self._sums[i]
            buf.append((delta, volume))
            sums[0] += delta
            sums[1] += volume
            if len(buf) > k:
                old_delta, old_volume = buf.popleft()
                sums[0] -= old_delta
                sums[1] -= old_volume
            if len(buf) < k:
                complete = False
                continue
            if sums[1] <= 0.0:
                complete = False
                continue
            ratios.append(sums[0] / sums[1])
        if not complete or len(ratios) != len(self.windows):
            return

        consensus = sum(ratios) / float(len(ratios))

        # The scale accumulates through warmup as well: those bars are real,
        # only the orders would not be, and reaching the first actionable bar
        # with no idea what "large" means here would waste the warmup.
        self._push(consensus)
        self._bars += 1
        if self._bars % 512 == 0:
            self._resync()

        if not ctx.actionable:
            return

        if self.cooldown > 0:
            self.cooldown -= 1
        if ctx.working_order_ids:
            return

        position = ctx.position
        if position > 0:
            self.dir = 1
        elif position < 0:
            self.dir = -1
        else:
            self.dir = 0

        # ---- catastrophe stop: insurance, checked on closes ------------------
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
                    self._held = 0
                    return

        z = self._z()
        if z is None:
            return

        self._held += 1

        # ---- long: hold until the opposite flow is worth a round trip --------
        if position > 0:
            if z < -self.flip_z and self._held >= self.min_hold:
                # One order takes the whole position through zero to -size:
                # no bar spent flat, no half-applied flip if only the close
                # had filled.
                if ctx.sell(size=position + self.size, tag="flip-short") is not None:
                    self.dir = -1
                    self._held = 0
            return

        # ---- short: symmetric ------------------------------------------------
        if position < 0:
            if z > self.flip_z and self._held >= self.min_hold:
                if ctx.buy(size=-position + self.size, tag="flip-long") is not None:
                    self.dir = 1
                    self._held = 0
            return

        # ---- flat: enter on the sign of the flow -----------------------------
        # Being flat is the one state the exposure floor forbids, and a book
        # held flat on purpose has no whipsaw to protect against.
        if self.cooldown > 0:
            return
        if z > self.entry_z:
            if ctx.buy(size=self.size, tag="cvd-long") is not None:
                self.dir = 1
                self._held = 0
        elif z < -self.entry_z:
            if ctx.sell(size=self.size, tag="cvd-short") is not None:
                self.dir = -1
                self._held = 0

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx):
        # Keep the direction in step with what actually happened. No order id is
        # kept across callbacks, so no expiry can make one stale.
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

    def _push(self, value):
        if len(self._cons) >= self.sd_window:
            old = self._cons.popleft()
            self._sr -= old
            self._sr2 -= old * old
        self._cons.append(value)
        self._sr += value
        self._sr2 += value * value

    def _resync(self):
        self._sr = 0.0
        self._sr2 = 0.0
        for value in self._cons:
            self._sr += value
            self._sr2 += value * value
        for i, buf in enumerate(self._bufs):
            d = 0.0
            v = 0.0
            for bar_delta, bar_volume in buf:
                d += bar_delta
                v += bar_volume
            self._sums[i][0] = d
            self._sums[i][1] = v

    def _z(self):
        n = len(self._cons)
        if n < self.min_sd_obs:
            return None
        mean = self._sr / n
        var = self._sr2 / n - mean * mean
        if var <= 0.0:
            return None
        sd = math.sqrt(var)
        if sd < 1e-12:
            return None
        return self._cons[-1] / sd
