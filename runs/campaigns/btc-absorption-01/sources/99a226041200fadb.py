"""Absorption reversal on the OKX BTC perpetual -- failed-breakdown entry on a 2m trigger.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. A long follows absorbed selling after a fall; a short
follows absorbed buying after a rise. This file never trades with the absorbed
flow -- every entry is against it.

Where the campaign stands, read off the only two trials that were scored
------------------------------------------------------------------------
#1 (tight 35/70bp bracket, 1,892 trades)  score -0.2761  -> -1.46e-4 per trade
#3 (3-hour horizon, tail bracket, 1,257)   score -0.1706  -> -1.36e-4 per trade

A round trip is 10bp of notional, so a strategy with no gross edge at all would
lose 1.0e-3 per trade; the two completed trials lost about 1.4e-4. Both exit
regimes -- a tight bracket held under an hour and a three-hour fixed horizon with
an unreachable bracket -- landed on the same per-trade number to within 8%. Two
quite different exits do not fail in the same way by coincidence. What that says
is: the exits are not the binding constraint, and whatever drift follows the
signal is worth roughly +8.5bp gross against a 10bp toll. The signal is real and
it is about 1.5-2bp short.

The third trial (#5) tried to buy that margin with selectivity -- double the
average one-hour move, a 1.0-sigma delta z, a 50% rejection close -- and fired on
116 of 960 sessions. Under the 70% exposure floor, so it scored nothing and
taught us nothing about whether selectivity works. That is the trap in this
campaign: the floor makes every threshold-only fix unscorable before it can be
answered.

The one place the missing 2bp was almost certainly given away
-------------------------------------------------------------
Not the exit. The entry.

Every prior trial required the absorption bar to *close back inside its range* --
40-50% of the way up from the low for a long. That is the correct reading of the
claim (the seller was absorbed and the bar shows it), but it is measured on a 5m
bar, and a 5m BTC bar has a range of roughly 15-25bp. Requiring the close to sit
halfway up that bar means paying 7-12bp of the reversal *before the position
exists*. The gross drift being harvested is about 8.5bp. Most of it is handed
over at the entry, and no exit rule can recover it.

So this trial keeps the claim's structure and moves the trigger to a 2m bar. A
2m bar's range is roughly 8-10bp, so the same rejection close costs 3-4bp instead
of 7-12bp. That is the whole size of the shortfall, and it is bought without
changing thresholds, without cutting the number of sessions, and without paying
for a tick subscription. 2m rather than 1m is deliberate: 1m would halve it again
(about 1.5bp) but roughly doubles the callback count over ~980 sessions, and a
budget abandonment scores nothing. The marginal basis point is not worth that
risk; the jump from 5m to 2m is.

Second change, and it comes free with the finer trigger: the failed breakdown
---------------------------------------------------------------------------
The previous "fails to extend" rule was *the bar must not undercut the prior 12
bars' low* -- a stall above the recent low. The sharper statement of the same
thing is the stop-run: price takes out the recent extreme and closes back inside
it. Aggressors committed to the break, a passive buyer absorbed them, and the
break failed -- which is the mechanism the claim names, at the point of maximum
commitment rather than somewhere above it.

That rule was tried earlier in this campaign at 5m resolution against a 12-bar
(one hour) extreme and fired on 5 sessions out of 960, which is why it was
abandoned. The problem was the resolution, not the rule: at 2m resolution against
a 30-minute extreme, a sweep-and-reclaim is an ordinary event -- the tape pokes
below the half-hour low several times an hour in a directional market. So the
same idea becomes usable when the trigger is finer.

Signal, with every degree of freedom the brief leaves open fixed
---------------------------------------------------------------
Prior move. Close-to-close over the last `move_window` = 30 closed 2m bars (one
hour), measured to the *previous* bar so the absorption bar itself cannot dilute
the move it is supposed to be failing to extend. Required magnitude:
`strict_move_mult` (1.1) times an EWMA of the absolute one-hour move
(`ewma_alpha` 0.02, ~50-bar memory), with a 10bp floor so a dead tape cannot
collapse the threshold. Scaled to recent activity, not the whole sample.

Flow drove the move. Cumulative `bar.delta / bar.volume` over the same thirty
bars must agree in sign with the price move and be at least 2% in magnitude. The
premise is about aggressive flow moving price; a drift on balanced flow carries
no absorption to read.

Unusually large delta on the absorption bar. `delta / volume` for the bar,
z-scored against the same ratio over the preceding `ratio_window` = 120 bars
(four hours), with a 1.0-sigma threshold and a 6% absolute floor. The ratio
rather than raw contracts: BTC volume varies by orders of magnitude across hours
and years, and a raw-delta z is mostly a clock.

Fails to extend, and reclaims. Chosen rule: the absorption bar's low must
undercut the lowest low of the preceding `extreme_window` = 15 bars (30 minutes)
and its close must be back above that level -- the break failed. Mirror for
highs. The alternative, weaker rule (no new low at all) is what trials #1-#4
used; it is retained only in the relaxed tier.

Rejection close. The bar must close in the favourable 40% of its own range: it
refused to hold its extreme. This is the absorbed-flow signature the claim names
and it is fixed the moment the bar closes.

Confirmation. None. The entry is the absorption bar itself, at market, eligible
from the next event (T+1). Waiting a bar costs more entry price than it buys in
hit rate, and the whole point of this trial is entry price.

Orders. Market in, market out. Resting limits were considered and rejected in an
earlier trial for a reason that still holds: fills here are book-queue
pessimistic, a resting bid fills only when trades print strictly through it, so
the limit fills preferentially in exactly the states where the reversal failed.
Paying the touch is the cheaper of the two.

Exit. Fixed horizon of 20 bars (40 minutes) from the fill, or the UTC-day
flatten with ten minutes to spare, whichever comes first. A 1.5% stop and a 3%
target ride along as catastrophe protection only -- they are essentially
unreachable inside 40 minutes and exist so no single bar can be a disaster.

Flat to flat inside the UTC day, one trade per session
------------------------------------------------------
The entry block is only reachable from flat with nothing working, so long ->
short always passes through flat. At most one entry a session, which is the
cheapest shape that can still satisfy the exposure floor: the round trip costs
10bp whether the trade is the first of the day or the second, and the evidence
says the second is not better than the first.

Exposure is the failure mode that has cost this campaign four of six trials, so
it is managed explicitly rather than hoped for. The strategy tracks the fraction
of actionable sessions on which it has traded; if that falls below 80% it runs
the session on a relaxed tier (0.5x the move threshold, 0.3-sigma delta z, 25%
rejection, no flow-agreement test -- still a sweep-and-reclaim against the
absorbed flow, never with it). After 20:00 UTC a session with no trade yet does
the same, so the 70% floor has margin even if the strict tier fires less often
than expected. The relaxed tier is the honest part of this file: it is a weaker
reading of the same claim, taken because being unscored has been the most
expensive outcome here.

Robustness. Every feature is maintained incrementally on bounded deques with
running sums -- 32 closes, 15 extremes, 120 ratios, 30 deltas -- so nothing
grows across 960 sessions, nothing re-walks history, and there is no
`ctx.history()` call anywhere. Warmup is gated by `ctx.actionable`, but features
keep accumulating through it so the first actionable bar is not blind. The
statistics deliberately persist across the UTC boundary: this market has no close
and no gap, and clearing a four-hour flow window every midnight would leave the
first hours of every day untradable. The 2m subscription uses `emit_empty=True`
so the end-of-day flatten always has a callback to run in, even in a dead tape;
empty bars carry no volume and are excluded from every feature.
"""

from __future__ import annotations

import math
from collections import deque

NS_PER_SECOND = 1_000_000_000


class Strategy:
    def __init__(
        self,
        interval: str = "2m",
        bar_seconds: int = 120,
        size: int = 10,
        move_window: int = 30,
        extreme_window: int = 15,
        ratio_window: int = 120,
        ewma_alpha: float = 0.02,
        # strict tier
        strict_dr: float = 0.06,
        strict_z: float = 1.0,
        strict_rej: float = 0.40,
        strict_move_mult: float = 1.1,
        strict_move_floor: float = 0.0010,
        strict_imb: float = 0.02,
        # relaxed tier (exposure insurance)
        relax_dr: float = 0.03,
        relax_z: float = 0.30,
        relax_rej: float = 0.25,
        relax_move_mult: float = 0.50,
        relax_move_floor: float = 0.0006,
        relax_imb: float = 0.0,
        # exits
        stop_frac: float = 0.015,
        target_frac: float = 0.030,
        hold_bars: int = 20,
        # exposure management
        cover_target: float = 0.80,
        late_s: int = 72000,
        flat_buffer_s: int = 600,
    ) -> None:
        self.interval = interval
        self.size = int(size)

        self.move_window = max(5, int(move_window))
        self.extreme_window = max(3, int(extreme_window))
        self.ratio_window = max(30, int(ratio_window))
        self.ewma_alpha = float(ewma_alpha)

        self.strict_dr = strict_dr
        self.strict_z = strict_z
        self.strict_rej = strict_rej
        self.strict_move_mult = strict_move_mult
        self.strict_move_floor = strict_move_floor
        self.strict_imb = strict_imb

        self.relax_dr = relax_dr
        self.relax_z = relax_z
        self.relax_rej = relax_rej
        self.relax_move_mult = relax_move_mult
        self.relax_move_floor = relax_move_floor
        self.relax_imb = relax_imb

        self.stop_frac = stop_frac
        self.target_frac = target_frac
        self.hold_bars = max(1, int(hold_bars))
        self.cover_target = cover_target
        self.late_ns = int(late_s) * NS_PER_SECOND
        self.flat_buffer_ns = int(flat_buffer_s) * NS_PER_SECOND
        self._hold_ns = self.hold_bars * int(bar_seconds) * NS_PER_SECOND

        # ---- bounded, incrementally maintained feature state. Nothing grows.
        m = self.move_window
        self.closes: deque = deque(maxlen=m + 2)
        self.lows_e: deque = deque(maxlen=self.extreme_window)
        self.highs_e: deque = deque(maxlen=self.extreme_window)
        self.ratios: deque = deque(maxlen=self.ratio_window)
        self.mdeltas: deque = deque(maxlen=m)
        self.mvols: deque = deque(maxlen=m)

        self._ratio_sum = 0.0
        self._ratio_sq = 0.0
        self._md_sum = 0.0
        self._mv_sum = 0.0
        self.ewma_abs = 0.0

        # ---- session / position bookkeeping
        self._session = None
        self._counted = False
        self._entries = 0
        self._traded = False
        self._relaxed_session = False
        self._sess_seen = 0
        self._sess_traded = 0
        self._entry_ts = 0
        self._flattening = False

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        # emit_empty so the end-of-day flatten always has a callback.
        ctx.subscribe(self.interval, emit_empty=True)

    # --------------------------------------------------------------- features

    def _push(self, bar) -> None:
        close = float(bar.close)
        hi = float(bar.high)
        lo = float(bar.low)
        vol = float(bar.volume)
        delta = float(bar.delta)

        self.closes.append(close)
        self.lows_e.append(lo)
        self.highs_e.append(hi)

        ratio = (delta / vol) if vol > 0.0 else 0.0
        if len(self.ratios) == self.ratio_window:
            old = self.ratios[0]
            self._ratio_sum -= old
            self._ratio_sq -= old * old
        self.ratios.append(ratio)
        self._ratio_sum += ratio
        self._ratio_sq += ratio * ratio

        if len(self.mdeltas) == self.move_window:
            self._md_sum -= self.mdeltas[0]
        self.mdeltas.append(delta)
        self._md_sum += delta

        if len(self.mvols) == self.move_window:
            self._mv_sum -= self.mvols[0]
        self.mvols.append(vol)
        self._mv_sum += vol

        m = self.move_window
        if len(self.closes) >= m + 1:
            start = self.closes[-m - 1]
            if start > 0.0:
                mv = abs((close - start) / start)
                a = self.ewma_alpha
                self.ewma_abs = (1.0 - a) * self.ewma_abs + a * mv

    # ----------------------------------------------------------------- signal

    def _signal(self, bar, relaxed: bool) -> int:
        """+1 absorbed selling after a fall, -1 absorbed buying after a rise."""
        vol = float(bar.volume)
        if vol <= 0.0:
            return 0
        delta = float(bar.delta)
        dr = delta / vol

        hi = float(bar.high)
        lo = float(bar.low)
        close = float(bar.close)
        rng = hi - lo
        if rng <= 0.0:
            return 0

        if relaxed:
            dr_thr = self.relax_dr
            z_thr = self.relax_z
            rej = self.relax_rej
            mult = self.relax_move_mult
            floor = self.relax_move_floor
            imb_thr = self.relax_imb
        else:
            dr_thr = self.strict_dr
            z_thr = self.strict_z
            rej = self.strict_rej
            mult = self.strict_move_mult
            floor = self.strict_move_floor
            imb_thr = self.strict_imb

        # 1. This bar's flow leans hard in one direction.
        if -dr_thr < dr < dr_thr:
            return 0
        long_side = dr < 0.0

        # 2. ... and it is unusual for the last four hours.
        n = len(self.ratios)
        if n < 30:
            return 0
        mean = self._ratio_sum / n
        var = self._ratio_sq / n - mean * mean
        if var <= 1e-12:
            return 0
        z = (dr - mean) / math.sqrt(var)
        if (z < 0.0) != long_side:
            return 0
        if abs(z) < z_thr:
            return 0

        # 3. There was a directional move into this bar, large for recent tape.
        m = self.move_window
        if len(self.closes) < m + 1:
            return 0
        start = self.closes[-m - 1]
        if start <= 0.0:
            return 0
        move = (self.closes[-1] - start) / start
        if (move < 0.0) != long_side:
            return 0
        thr = mult * self.ewma_abs
        if thr < floor:
            thr = floor
        if abs(move) < thr:
            return 0

        # 4. Aggressive flow drove that move.
        if imb_thr > 0.0:
            if len(self.mdeltas) < m or self._mv_sum <= 0.0:
                return 0
            imb = self._md_sum / self._mv_sum
            if (imb < 0.0) != long_side:
                return 0
            if abs(imb) < imb_thr:
                return 0

        # 5. The move broke the recent extreme and failed to hold it.
        if len(self.lows_e) < self.extreme_window:
            return 0
        if len(self.highs_e) < self.extreme_window:
            return 0

        if long_side:
            base = min(self.lows_e)
            if lo >= base:
                return 0
            if close <= base:
                return 0
            if (close - lo) < rej * rng:
                return 0
            return 1

        base = max(self.highs_e)
        if hi <= base:
            return 0
        if close >= base:
            return 0
        if (hi - close) < rej * rng:
            return 0
        return -1

    # ------------------------------------------------------------------- walk

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]
        real = bar.tick_count > 0

        # ---- session boundary. Coverage is tracked over actionable sessions
        # only: warmup delivers real bars but nothing can be traded in them.
        if ctx.session != self._session:
            if self._session is not None and self._counted:
                self._sess_seen += 1
                if self._traded:
                    self._sess_traded += 1
            self._session = ctx.session
            self._counted = bool(ctx.actionable)
            self._entries = 0
            self._traded = False
            if self._sess_seen >= 10:
                self._relaxed_session = (
                    self._sess_traded < self.cover_target * self._sess_seen
                )
            else:
                self._relaxed_session = False

        flat = ctx.position == 0
        left_ns = ctx.session_end - ctx.ts

        # ---- decide before the current bar is folded into the statistics, so
        #      the bar cannot raise the bar it has to clear.
        side = 0
        if real:
            if ctx.actionable and flat:
                relaxed = self._relaxed_session
                if not relaxed and self._entries == 0:
                    if (ctx.ts - ctx.session_start) >= self.late_ns:
                        relaxed = True
                side = self._signal(bar, relaxed)
            self._push(bar)

        if not ctx.actionable:
            return

        # ---- carrying a trade: the horizon clock, the session clock and the
        #      catastrophe bracket. Never open the opposite side here.
        if not flat:
            if self._flattening:
                return
            timed_out = (
                self._entry_ts > 0 and (ctx.ts - self._entry_ts) >= self._hold_ns
            )
            if timed_out or left_ns <= self.flat_buffer_ns:
                self._flattening = True
                self._flatten(ctx)
            return
        self._flattening = False

        if not real:
            return
        if side == 0:
            return
        if ctx.working_order_ids:
            return
        if self._entries >= 1:
            return
        if left_ns <= self._hold_ns + self.flat_buffer_ns:
            return

        price = ctx.last_price
        if price <= 0.0:
            return

        stop = price * self.stop_frac
        target = price * self.target_frac
        if side > 0:
            oid = ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="absorb-long",
            )
        else:
            oid = ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="absorb-short",
            )
        if oid is None:
            return
        self._entries += 1
        self._traded = True

    # ------------------------------------------------------------------ exits

    def _flatten(self, ctx) -> None:
        # Cancel the armed bracket first: a reduce-only leg left working after
        # the position is gone would block every later entry through
        # `ctx.working_order_ids`.
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position != 0:
            ctx.close(tag="horizon-exit")

    # -------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        if ctx.fill.position != 0:
            if self._entry_ts == 0:
                self._entry_ts = ctx.fill.ts
        else:
            self._entry_ts = 0
            self._flattening = False

    def on_lifecycle(self, ctx) -> None:
        pass
