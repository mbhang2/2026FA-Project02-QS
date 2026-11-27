"""Absorption reversal on the OKX BTC perpetual -- buy the weak close at support.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that fails to extend price marks absorption,
and price tends to turn. Long follows absorbed selling after a fall; short
follows absorbed buying after a rise. The strategy never trades with the
absorbed flow.

What the two completed trials actually said
-------------------------------------------
#1 (tight 35bp/70bp bracket, 1,892 trades) scored -0.2761.
#3 (fixed 3h horizon, 2%/6% tail bracket, 1,257 trades) scored -0.1706.

Per trade that is -1.46e-4 and -1.36e-4 respectively -- the same number, to
within 8%, from two completely different exit regimes and two different signal
constructions. A strategy with zero gross edge loses exactly the round-trip
cost per trade, and 10bp of cost is roughly 1.4e-4 of this score. So both
trials are consistent with **gross edge = 0**: not with a bad exit, and not
with a stop that is too tight. Changing the exit again is not the experiment
worth spending.

The question is therefore the entry, and there are two specific reasons the
prior entries should have carried no information.

1. **The prior move was too small to reverse into a profit.** #3 required a
   12-bar (one hour) close-to-close move of at least max(0.8 sigma_1h, 15bp).
   BTC's one-hour return standard deviation in this sample is around 50bp, so
   0.8 sigma is ~40bp and the 15bp floor was the binding number most of the
   time. A typical one-hour drift of 15bp on BTC is noise; whatever mean
   reversion it has is a few basis points, an order of magnitude under the
   toll. Only *unusual* moves have a crowd to trap, and only they can produce
   a reversal larger than 10bp plus spread. This trial requires roughly two
   times the recent average absolute one-hour move, and scales that average
   with a decayed estimate so that a quiet tape still produces a signal that
   is unusual *for that tape* -- an absolute floor alone would leave the
   quiet sessions untraded and breach the exposure floor, which is what
   happened to trials #0 and #2.

2. **The prior entry bought after the bounce.** Both completed trials demanded
   that the absorption bar close in the upper (lower) 30-40% of its own range
   before going long (short). On a five-minute bar that means entering after
   the market has already retraced most of the bar against the sellers -- the
   reversion has happened, and the strategy pays the offer for the part that
   is left. That is a coherent reason for gross edge to come out at exactly
   zero rather than negative: the entry captures the tail of the move it
   predicted.

   This trial inverts that condition. Long requires the absorption bar to
   close in the **lower half** of its own range: still weak, still being sold,
   but with its low holding above the low of the previous twelve bars. The
   entry is at market into that weakness, at support, before the bounce rather
   than after it. Stop placement follows from the same object -- just under
   the prior low that the bar refused to break.

Signal, with every degree of freedom the brief leaves open fixed
---------------------------------------------------------------
Prior move. Close-to-close over the last `move_window` (12) closed 5m bars --
one hour. Required magnitude: `ewma_mult` (2.0) times a decayed estimate of
the average absolute one-hour move (`ewma_alpha` 0.02, ~50-bar memory), with
an 8bp absolute floor so the threshold cannot collapse to nothing. Scaled to
recent activity, not to the whole sample, so it fires in quiet regimes too.

Flow drove the move. Cumulative `bar.delta / bar.volume` over the same twelve
bars must agree in sign with the price move and be at least 3% in magnitude.
The premise is about aggressive flow moving price; a drift on balanced flow
carries no absorption to read.

Unusually large delta on the absorption bar. `delta / volume` for the bar,
z-scored against the same ratio over the preceding 96 bars (eight hours), with
a 1.0 sigma threshold and a 5% absolute floor. The ratio rather than raw
contracts: BTC volume varies by orders of magnitude across hours and years.

Fails to extend. The bar's low must not undercut the lowest low of the
preceding twelve bars (mirror for the high). This is the direct reading of
"the flow moved nothing". The sweep-and-reclaim variant was tried earlier in
this campaign and fired on 5 sessions out of 960, far under the exposure
floor, so the non-extension form is used.

Weak close. The bar must close in the lower (upper) half of its own range --
the opposite of the earlier trials, and the whole point of this proposal. See
above.

Confirmation. None. The entry is the absorption bar itself, market, eligible
from the next event (T+1). Waiting a bar costs the entry price, which is the
one thing this trial is trying to stop giving away.

Orders. Market in, market out. Resting limits were considered and rejected:
a resting bid fills only when trades print strictly through it, so it fills
preferentially in exactly the states where the reversal failed. The entry
this trial wants is the weak close at support, and that is a market buy.

Exit. A catastrophic stop placed just under the prior low (bounded to
[1.0, 2.5] ATR), a 4 ATR target that only trims the tail, and otherwise a
fixed horizon of 18 bars (90 minutes). The horizon is deliberately shorter
than trial #3's three hours: the reversion sought here is the one attached to
the absorption print itself, and past that the position is just carrying
unrelated drift. Everything is flat before the UTC close.

Flat to flat, within the UTC day. One position at a time; the entry block is
only reachable from flat with nothing working, so long -> short is always
expressed as long -> flat -> short. Entries are refused with less than 105
minutes left, and any open position is cancelled and closed with 15 minutes
left. The 5m subscription uses `emit_empty=True` so the end-of-day exit always
has a callback even in a dead tape. At most two entries a session, at least
two hours apart.

Robustness. Every feature is maintained incrementally on bounded deques with
running sums; nothing grows across 960 sessions and nothing re-walks history,
so no `WindowTooShort` and no quadratic cost. No `ctx.history()` call is made
at all. Warmup is skipped via `ctx.actionable`, but features keep accumulating
through it. The statistics deliberately persist across the UTC boundary: this
market has no close and no gap, and clearing a one-hour flow window every
midnight would leave the first hours of every day untradable.
"""

from __future__ import annotations

import math
from collections import deque

NS_PER_SECOND = 1_000_000_000


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        size: int = 10,
        move_window: int = 12,
        atr_bars: int = 24,
        ratio_window: int = 96,
        ewma_alpha: float = 0.02,
        ewma_mult: float = 2.0,
        move_floor: float = 0.0008,
        imb_thr: float = 0.03,
        dr_thr: float = 0.05,
        delta_z: float = 1.0,
        weak_close_frac: float = 0.50,
        stop_add_atr: float = 0.75,
        stop_min_atr: float = 1.0,
        stop_max_atr: float = 2.5,
        target_atr: float = 4.0,
        hold_bars: int = 18,
        max_entries: int = 2,
        cooldown_bars: int = 24,
        flat_buffer_s: int = 900,
    ) -> None:
        self.interval = interval
        self.size = size
        self.move_window = move_window
        self.atr_bars = atr_bars
        self.ratio_window = ratio_window
        self.ewma_alpha = ewma_alpha
        self.ewma_mult = ewma_mult
        self.move_floor = move_floor
        self.imb_thr = imb_thr
        self.dr_thr = dr_thr
        self.delta_z = delta_z
        self.weak_close_frac = weak_close_frac
        self.stop_add_atr = stop_add_atr
        self.stop_min_atr = stop_min_atr
        self.stop_max_atr = stop_max_atr
        self.target_atr = target_atr
        self.hold_bars = hold_bars
        self.max_entries = max_entries
        self.cooldown_bars = cooldown_bars
        self.flat_buffer_ns = flat_buffer_s * NS_PER_SECOND

        m = max(2, int(move_window))
        self.move_window = m

        # Bounded, incrementally maintained feature state.
        self.closes: deque = deque(maxlen=m + 2)
        self.highs: deque = deque(maxlen=m + 1)
        self.lows: deque = deque(maxlen=m + 1)
        self.ratios: deque = deque(maxlen=max(20, int(ratio_window)))
        self.mdeltas: deque = deque(maxlen=m)
        self.mvols: deque = deque(maxlen=m)
        self.ranges: deque = deque(maxlen=max(2, int(atr_bars)))

        self._ratio_sum = 0.0
        self._ratio_sq = 0.0
        self._md_sum = 0.0
        self._mv_sum = 0.0
        self._rng_sum = 0.0

        self.last_ratio = 0.0
        self.last_range = 0.0
        self.ewma_abs = 0.0
        self.move_thr = move_floor

        # Session / trade bookkeeping.
        self._session = None
        self._entries = 0
        self._bar = 0
        self._last_entry_bar = -(10 ** 9)
        self._held = 0

        # An entry needs a full horizon plus the flatten buffer to still be
        # inside the UTC day; the 5m bar is 300 seconds.
        self._min_left_ns = (int(hold_bars) * 300 + int(flat_buffer_s)) * NS_PER_SECOND

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        # emit_empty so the end-of-session flatten always has a callback.
        ctx.subscribe(self.interval, emit_empty=True)

    # --------------------------------------------------------------- features

    def _push(self, dq: deque, val: float, attr: str, sq: str = "") -> None:
        """Append to a bounded deque, maintaining running sum (and squares)."""
        if len(dq) == dq.maxlen:
            old = dq[0]
            setattr(self, attr, getattr(self, attr) - old)
            if sq:
                setattr(self, sq, getattr(self, sq) - old * old)
        dq.append(val)
        setattr(self, attr, getattr(self, attr) + val)
        if sq:
            setattr(self, sq, getattr(self, sq) + val * val)

    def _update(self, bar) -> None:
        close = float(bar.close)
        hi = float(bar.high)
        lo = float(bar.low)
        vol = float(bar.volume)
        delta = float(bar.delta)

        self.last_range = hi - lo

        # Freeze the move threshold against the estimate *before* this bar
        # contributes to it, so a large move cannot raise the bar it must
        # itself clear.
        thr = self.ewma_mult * self.ewma_abs
        self.move_thr = thr if thr > self.move_floor else self.move_floor

        self.closes.append(close)
        self.highs.append(hi)
        self.lows.append(lo)

        ratio = (delta / vol) if vol > 0.0 else 0.0
        self.last_ratio = ratio
        self._push(self.ratios, ratio, "_ratio_sum", "_ratio_sq")
        self._push(self.mdeltas, delta, "_md_sum")
        self._push(self.mvols, vol, "_mv_sum")
        self._push(self.ranges, hi - lo, "_rng_sum")

        m = self.move_window
        if len(self.closes) >= m + 1:
            start = self.closes[-m - 1]
            if start > 0.0:
                mv = abs((close - start) / start)
                a = self.ewma_alpha
                self.ewma_abs = (1.0 - a) * self.ewma_abs + a * mv

    # ----------------------------------------------------------------- signal

    def _signal(self) -> int:
        """+1 absorbed selling after a fall, -1 absorbed buying after a rise."""
        m = self.move_window
        if len(self.closes) < m + 1:
            return 0
        if len(self.lows) < m + 1:
            return 0
        if len(self.mdeltas) < m:
            return 0
        if len(self.ranges) < self.atr_bars:
            return 0
        if len(self.ratios) < 20:
            return 0

        rng = self.last_range
        if rng <= 0.0:
            return 0

        # 1. An unusually large prior move, scaled to recent activity.
        start = self.closes[-m - 1]
        if start <= 0.0:
            return 0
        move = (self.closes[-1] - start) / start
        if abs(move) < self.move_thr:
            return 0

        # 2. This bar's flow leans the same way, and unusually so.
        dr = self.last_ratio
        if -self.dr_thr < dr < self.dr_thr:
            return 0
        long_side = dr < 0.0
        if (move < 0.0) != long_side:
            return 0

        n = len(self.ratios)
        mean = self._ratio_sum / n
        var = self._ratio_sq / n - mean * mean
        if var <= 1e-12:
            return 0
        z = (dr - mean) / math.sqrt(var)
        if (z < 0.0) != long_side:
            return 0
        if abs(z) < self.delta_z:
            return 0

        # 3. Aggressive flow drove the prior move.
        if self._mv_sum <= 0.0:
            return 0
        imb = self._md_sum / self._mv_sum
        if abs(imb) < self.imb_thr:
            return 0
        if (imb < 0.0) != long_side:
            return 0

        # 4. The flow failed to extend price, and the bar closed weak into
        #    that support rather than bouncing off it.
        lows = list(self.lows)
        highs = list(self.highs)
        close = self.closes[-1]

        if long_side:
            base = min(lows[:-1])
            if lows[-1] < base:
                return 0
            if (close - lows[-1]) > self.weak_close_frac * rng:
                return 0
            return 1

        base = max(highs[:-1])
        if highs[-1] > base:
            return 0
        if (highs[-1] - close) > self.weak_close_frac * rng:
            return 0
        return -1

    # ------------------------------------------------------------------- walk

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._session = ctx.session
            self._entries = 0
            self._held = 0

        real = bar.tick_count > 0
        if real:
            self._update(bar)
            self._bar += 1

        if not ctx.actionable:
            return

        left_ns = ctx.session_end - ctx.ts

        # ---- carrying a trade: the bracket, the fixed horizon and the session
        #      clock are the only exits. Never open the opposite side here.
        if ctx.position != 0:
            self._held += 1
            if self._held >= self.hold_bars or left_ns <= self.flat_buffer_ns:
                self._flatten(ctx)
            return
        self._held = 0

        if not real:
            return
        if ctx.working_order_ids:
            return
        if left_ns <= self._min_left_ns:
            return
        if self._entries >= self.max_entries:
            return
        if self._bar - self._last_entry_bar < self.cooldown_bars:
            return

        side = self._signal()
        if side == 0:
            return

        if not self.ranges:
            return
        atr = self._rng_sum / len(self.ranges)
        if atr <= 0.0:
            return

        close = self.closes[-1]
        lows = list(self.lows)
        highs = list(self.highs)
        if len(lows) < 2 or len(highs) < 2:
            return

        if side > 0:
            base = min(lows[:-1])
            dist = close - base
            if dist < 0.0:
                dist = 0.0
            stop = dist + self.stop_add_atr * atr
            if stop < self.stop_min_atr * atr:
                stop = self.stop_min_atr * atr
            if stop > self.stop_max_atr * atr:
                stop = self.stop_max_atr * atr
            oid = ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=self.target_atr * atr,
                tag="absorb-long",
            )
        else:
            base = max(highs[:-1])
            dist = base - close
            if dist < 0.0:
                dist = 0.0
            stop = dist + self.stop_add_atr * atr
            if stop < self.stop_min_atr * atr:
                stop = self.stop_min_atr * atr
            if stop > self.stop_max_atr * atr:
                stop = self.stop_max_atr * atr
            oid = ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=self.target_atr * atr,
                tag="absorb-short",
            )

        if oid is None:
            return
        self._entries += 1
        self._last_entry_bar = self._bar
        self._held = 0

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
        if ctx.fill.position == 0:
            self._held = 0

    def on_lifecycle(self, ctx) -> None:
        pass
