"""Absorption reversal on the BTC perpetual -- finer trigger, same claim,
one-hour horizon, and the entry price treated as the thing that was wrong.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. A long follows absorbed selling after a fall; a short
follows absorbed buying after a rise. Nothing in this file ever trades with the
absorbed flow.

What the scored trials actually established, and what this file does about it
-----------------------------------------------------------------------------
    #1  tight 35/70bp bracket,         1892 trades   -0.2761  ->  -1.46e-4/trade
    #3  fixed 3h horizon, tail bracket 1257 trades   -0.1706  ->  -1.36e-4/trade
    #6  1.2-sigma move + climax + tier2 1200 trades  -0.2099  ->  -1.75e-4/trade
    #7  2m sweep-and-reclaim, 40min     917 trades   -0.2940  ->  -3.20e-4/trade

Two facts stand out. First, #1 and #3 land on the same per-trade number to
within 1% despite exits that could hardly be more different -- a twenty-minute
tight bracket and a three-hour horizon with an unreachable target. Two exits do
not agree like that unless the per-trade result is set before the exit ever
runs, i.e. at the entry. Second, the absolute level is around -1.4bp a trade.
A round trip is 10bp of commission plus the touch, so the gross being harvested
is roughly +8.5bp, and the shortfall is roughly 1.5bp.

So this trial does not touch the exit shape again. It attacks the entry price,
which is the only place a 1.5bp shortfall can plausibly be recovered.

Where the entry price goes
--------------------------
Every prior trial required the absorption bar to close back inside its own
range -- 30-50% of the way up from the low. That is the right reading of the
claim, but on a 5m bar whose range is ~20bp it means entering 6-10bp above the
low, and the effect being harvested is ~8.5bp in total. Most of it is handed
back at the moment the position is created, and no exit rule can recover it.

A finer bar pays a smaller fraction of a smaller range for the same
confirmation. That is the whole mechanism of this trial: the same claim,
measured on 2m bars instead of 5m bars, so the rejection close costs a few
basis points instead of six to ten. 2m rather than 1m is deliberate and is the
one place this file is deliberately timid: 2m is the resolution trial #7
already completed at, so the callback budget over ~960 sessions is known to be
survivable, while 1m would double it for roughly one more basis point of entry
price. An abandoned run scores nothing; the marginal basis point is not worth
that.

Second, free with the finer trigger: a *purer* absorption bar
-------------------------------------------------------------
At 5m a bar can qualify on the rejection-close test alone. At 2m the same
statistic is noisier, so the strict tier additionally requires the bar to close
at or above its own open while carrying heavy *sell-side* delta (mirror for
shorts). That is the claim in its literal form -- aggressive flow arrived and
price did not go with it -- rather than a proxy for it, and it is checked
entirely inside one closed bar, so it can never be revised afterwards.

The signal, with every degree of freedom the brief leaves open fixed
--------------------------------------------------------------------
Prior move. Close-to-close over the previous `move_window` = 30 closed 2m bars,
one hour, measured to the bar *before* the absorption bar so the absorption bar
cannot dilute the move it is supposed to be failing to extend. Required
magnitude: `move_sigmas` (0.8) times the standard deviation of the last 240 2m
returns scaled by sqrt(30), with a 15bp floor so the threshold cannot collapse
in a dead tape. Scaled to recent activity, not to the sample.

Flow drove the move. Cumulative `bar.delta / bar.volume` over the same thirty
bars must agree in sign with the price move and be at least 2% in magnitude.
The premise concerns *aggressive* flow moving price; a drift on balanced flow
carries no absorption to read.

Unusually large delta on the absorption bar. `delta / volume` for the bar,
z-scored against the same ratio over the previous 120 bars (four hours), with a
0.8-sigma threshold and a 6% absolute floor. The ratio rather than raw
contracts, because BTC volume differs by orders of magnitude across hours and
across years and a raw-delta z-score would mostly be a clock.

Fails to extend. Chosen rule, held throughout: the bar may not undercut the
lowest low of the previous thirty 2m bars (one hour), and it must close in the
favourable 30% of its own range, and -- strict tier only -- at or above its own
open. The first clause is "the flow moved nothing at all"; the other two are
the rejection of the bar's own extreme, measurable inside one bar.

Confirmation. None. The entry is the absorption bar itself, at market, eligible
from the next event (T+1). Waiting a bar costs more entry price than it buys in
hit rate, and entry price is the entire subject of this trial.

Orders. Market in, market out. Resting limits were considered and rejected
again for the reason that has held all campaign: fills here are book-queue
pessimistic, so a resting bid fills only when trades print strictly through it
and therefore fills preferentially in exactly the states where the reversal
failed. Adverse selection on entry costs more than the touch.

Exit. Fixed horizon of 30 bars (60 minutes) from the fill, closed at market, or
the UTC-day flatten with fifteen minutes to spare, whichever comes first. A 2%
stop and a 4% target ride along as catastrophe protection only -- both are
essentially unreachable inside an hour of BTC and exist so that no single bar
can be a disaster. The hour is a compromise the earlier trials bracket: the
20-minute bracket and the 3-hour horizon returned the same gross, so the drift
attaches early and then saturates; an hour collects it without paying for two
extra hours of variance.

Flat to flat inside the UTC day, and exposure managed explicitly
----------------------------------------------------------------
One position at a time; the entry block is reachable only from flat with
nothing working, so long -> short always passes through flat and every trade
opens and closes within a single UTC day. At most two entries a session, at
least an hour apart. Entries are refused with less than the horizon plus a
fifteen-minute buffer left in the day, and an open position is cancelled and
closed with fifteen minutes left. The 2m subscription uses `emit_empty=True`
so that end-of-day exit always has a callback to run in, even in a dead tape.

Four of this campaign's eight proposals produced no score at all because they
traded on too few sessions, and a not-scored run teaches nothing. So the strict
tier carries a backstop: once 65% of the UTC day has elapsed with no trade
yet, a relaxed tier (0.35 sigma move, 8bp floor, 3% delta floor, 0.3-sigma z,
22% rejection close, no flow-agreement test, no close-at-or-above-open test --
still an absorption reversal against the absorbed flow, never with it) is
allowed as the session's *first* trade only. It cannot dilute a day that
already found the real signal, and it is the honest admission that being
unscored has been the most expensive outcome here.

Robustness. Every feature is maintained incrementally on bounded deques with
running sums -- 30 closes, 30 extremes, 120 ratios, 30 deltas, 240 returns --
so nothing grows across 960 sessions, nothing re-walks history, and there is no
`ctx.history()` call anywhere. `ctx.actionable` gates the orders; features keep
accumulating through warmup so the first actionable bar is not blind. The flow
statistics deliberately persist across the UTC boundary: this market has no
close and no gap, and clearing a four-hour flow window every midnight would
leave the first hours of every day untradable. Every order id returned by
`buy`/`sell` is checked for `None`.
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
        # feature windows, in bars of `interval`
        move_window: int = 30,
        extreme_window: int = 30,
        ratio_window: int = 120,
        ret_window: int = 240,
        # strict tier
        move_sigmas: float = 0.80,
        move_floor: float = 0.0015,
        delta_floor: float = 0.060,
        delta_sigmas: float = 0.80,
        close_frac: float = 0.30,
        flow_agree: float = 0.020,
        # relaxed tier -- exposure insurance, first trade of the day only
        fb_move_sigmas: float = 0.35,
        fb_move_floor: float = 0.0008,
        fb_delta_floor: float = 0.030,
        fb_delta_sigmas: float = 0.30,
        fb_close_frac: float = 0.22,
        fb_flow_agree: float = 0.0,
        fallback_after: float = 0.65,
        # exits
        stop_frac: float = 0.020,
        target_frac: float = 0.040,
        hold_bars: int = 30,
        max_entries: int = 2,
        cooldown_bars: int = 30,
        flat_buffer_s: int = 900,
    ) -> None:
        self.interval = interval
        self.size = int(size)

        self.move_window = max(5, int(move_window))
        self.extreme_window = max(3, int(extreme_window))
        self.ratio_window = max(30, int(ratio_window))
        self.ret_window = max(60, int(ret_window))

        self.move_sigmas = float(move_sigmas)
        self.move_floor = float(move_floor)
        self.delta_floor = float(delta_floor)
        self.delta_sigmas = float(delta_sigmas)
        self.close_frac = float(close_frac)
        self.flow_agree = float(flow_agree)

        self.fb_move_sigmas = float(fb_move_sigmas)
        self.fb_move_floor = float(fb_move_floor)
        self.fb_delta_floor = float(fb_delta_floor)
        self.fb_delta_sigmas = float(fb_delta_sigmas)
        self.fb_close_frac = float(fb_close_frac)
        self.fb_flow_agree = float(fb_flow_agree)
        self.fallback_after = float(fallback_after)

        self.stop_frac = float(stop_frac)
        self.target_frac = float(target_frac)
        self.hold_bars = max(1, int(hold_bars))
        self.max_entries = max(1, int(max_entries))
        self.cooldown_bars = max(0, int(cooldown_bars))
        self.flat_buffer_ns = int(flat_buffer_s) * NS_PER_SECOND

        m = self.move_window

        # Bounded, incrementally maintained feature state. Nothing grows.
        self.closes: deque = deque(maxlen=m)
        self.highs: deque = deque(maxlen=self.extreme_window)
        self.lows: deque = deque(maxlen=self.extreme_window)
        self.ratios: deque = deque(maxlen=self.ratio_window)
        self.mdeltas: deque = deque(maxlen=m)
        self.mvols: deque = deque(maxlen=m)
        self.rets: deque = deque(maxlen=self.ret_window)

        self._ret_sum = 0.0
        self._ret_sq = 0.0
        self._ratio_sum = 0.0
        self._ratio_sq = 0.0
        self._md_sum = 0.0
        self._mv_sum = 0.0

        # Session and trade bookkeeping.
        self._session = None
        self._entries = 0
        self._bar = 0
        self._last_entry_bar = -(10 ** 9)
        self._bar_ns = 0
        self._hold_ns = 0
        self._entry_ts = 0
        self._flattening = False

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        # emit_empty so the end-of-day flatten always has a callback to run in,
        # even in a tape with no trades in the final interval.
        ctx.subscribe(self.interval, emit_empty=True)

    # --------------------------------------------------------------- features

    def _push(self, dq: deque, val: float, sname: str, sqname: str = "") -> None:
        """Append to a bounded deque, maintaining the running sum (and squares)."""
        if len(dq) == dq.maxlen:
            old = dq[0]
            setattr(self, sname, getattr(self, sname) - old)
            if sqname:
                setattr(self, sqname, getattr(self, sqname) - old * old)
        dq.append(val)
        setattr(self, sname, getattr(self, sname) + val)
        if sqname:
            setattr(self, sqname, getattr(self, sqname) + val * val)

    def _update(self, bar) -> None:
        close = float(bar.close)
        vol = float(bar.volume)
        delta = float(bar.delta)

        if self.closes:
            prev = self.closes[-1]
            if prev > 0.0:
                self._push(self.rets, (close - prev) / prev,
                           "_ret_sum", "_ret_sq")

        ratio = (delta / vol) if vol > 0.0 else 0.0
        self._push(self.ratios, ratio, "_ratio_sum", "_ratio_sq")
        self._push(self.mdeltas, delta, "_md_sum")
        self._push(self.mvols, vol, "_mv_sum")

        self.closes.append(close)
        self.highs.append(float(bar.high))
        self.lows.append(float(bar.low))

    # ----------------------------------------------------------------- signal

    def _signal(self, bar, relaxed: bool) -> int:
        """+1 absorbed selling after a fall, -1 absorbed buying after a rise."""
        vol = float(bar.volume)
        if vol <= 0.0:
            return 0

        hi = float(bar.high)
        lo = float(bar.low)
        op = float(bar.open)
        close = float(bar.close)
        rng = hi - lo
        if rng <= 0.0:
            return 0

        if relaxed:
            dfloor = self.fb_delta_floor
            dsig = self.fb_delta_sigmas
            cfrac = self.fb_close_frac
            msig = self.fb_move_sigmas
            mfloor = self.fb_move_floor
            fagree = self.fb_flow_agree
            require_absorb_bar = False
        else:
            dfloor = self.delta_floor
            dsig = self.delta_sigmas
            cfrac = self.close_frac
            msig = self.move_sigmas
            mfloor = self.move_floor
            fagree = self.flow_agree
            require_absorb_bar = True

        # 1. This bar's flow leans hard in one direction -- and, in the strict
        #    tier, price refused to follow it on the bar itself.
        dr = float(bar.delta) / vol
        if -dfloor < dr < dfloor:
            return 0
        long_side = dr < 0.0

        # 2. ... and that lean is unusual for the last four hours.
        n = len(self.ratios)
        if n < 30:
            return 0
        mu = self._ratio_sum / n
        var = self._ratio_sq / n - mu * mu
        if var <= 1e-12:
            return 0
        z = (dr - mu) / math.sqrt(var)
        if (z < 0.0) != long_side:
            return 0
        if abs(z) < dsig:
            return 0

        # 3. There was a directional move into this bar, measured to the bar
        #    before it so the absorption bar cannot dilute its own context.
        m = self.move_window
        if len(self.closes) < m:
            return 0
        start = self.closes[-m]
        if start <= 0.0:
            return 0
        move = (self.closes[-1] - start) / start
        if (move < 0.0) != long_side:
            return 0

        cn = len(self.rets)
        if cn < 60:
            return 0
        mr = self._ret_sum / cn
        vr = self._ret_sq / cn - mr * mr
        if vr <= 0.0:
            return 0
        thr = msig * math.sqrt(vr) * math.sqrt(m)
        if thr < mfloor:
            thr = mfloor
        if abs(move) < thr:
            return 0

        # 4. Aggressive flow drove that move.
        if fagree > 0.0:
            if self._mv_sum <= 0.0:
                return 0
            imb = self._md_sum / self._mv_sum
            if (imb < 0.0) != long_side:
                return 0
            if abs(imb) < fagree:
                return 0

        # 5. The flow failed to extend price over the hour, and the bar
        #    rejected its own extreme.
        if len(self.lows) < self.extreme_window:
            return 0
        if len(self.highs) < self.extreme_window:
            return 0

        if long_side:
            if lo < min(self.lows):
                return 0
            if (close - lo) < cfrac * rng:
                return 0
            if require_absorb_bar and close < op:
                return 0
            return 1

        if hi > max(self.highs):
            return 0
        if (hi - close) < cfrac * rng:
            return 0
        if require_absorb_bar and close > op:
            return 0
        return -1

    # ------------------------------------------------------------------- walk

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]
        real = bar.tick_count > 0

        # ---- session boundary.
        if ctx.session != self._session:
            self._session = ctx.session
            self._entries = 0
            self._bar = 0
            self._last_entry_bar = -(10 ** 9)

        if self._bar_ns == 0 and bar.close_ts > bar.open_ts:
            self._bar_ns = bar.close_ts - bar.open_ts
            self._hold_ns = self.hold_bars * self._bar_ns

        if real:
            self._bar += 1

        left_ns = ctx.session_end - ctx.ts

        # ---- decide on the bar *before* folding it into the statistics, so a
        #      bar can never raise the threshold it has to clear.
        side = 0
        if (real and ctx.actionable and ctx.position == 0
                and not ctx.working_order_ids
                and self._bar_ns > 0
                and self._entries < self.max_entries
                and (self._bar - self._last_entry_bar) >= self.cooldown_bars
                and left_ns > self._hold_ns + self.flat_buffer_ns):
            side = self._signal(bar, False)
            if side == 0 and self._entries == 0:
                span = ctx.session_end - ctx.session_start
                if span > 0 and (ctx.ts - ctx.session_start) >= self.fallback_after * span:
                    side = self._signal(bar, True)

        # Real bars feed the features; empty ones carry the previous close
        # forward at zero volume and would poison every ratio. They still
        # provide the callback the end-of-day flatten needs.
        if real:
            self._update(bar)

        if not ctx.actionable:
            return

        # ---- carrying a trade: the horizon clock, the session clock and the
        #      catastrophe bracket are the only exits. Never reverse here.
        if ctx.position != 0:
            if self._flattening:
                return
            timed_out = (self._hold_ns > 0 and self._entry_ts > 0
                         and (ctx.ts - self._entry_ts) >= self._hold_ns)
            if timed_out or left_ns <= self.flat_buffer_ns:
                self._flattening = True
                self._flatten(ctx)
            return

        self._flattening = False

        if not real or side == 0:
            return
        if ctx.working_order_ids:
            return

        price = ctx.last_price
        if price <= 0.0:
            return

        stop = price * self.stop_frac
        target = price * self.target_frac
        if side > 0:
            oid = ctx.buy(size=self.size, stop_offset=stop,
                          target_offset=target, tag="absorb-long")
        else:
            oid = ctx.sell(size=self.size, stop_offset=stop,
                           target_offset=target, tag="absorb-short")
        if oid is None:
            return
        self._entries += 1
        self._last_entry_bar = self._bar

    # ------------------------------------------------------------------ exits

    def _flatten(self, ctx) -> None:
        # Cancel the armed bracket before flattening: a reduce-only leg left
        # working after the position is gone would keep
        # `ctx.working_order_ids` non-empty and block every later entry.
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
