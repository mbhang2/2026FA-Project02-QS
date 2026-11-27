"""Absorption reversal on the OKX BTCUSDT perpetual, measured as a flow-price
divergence rather than as a single-bar delta spike.

Claim under test (campaign question, 2026-11-22): after a directional move,
aggressive flow in that direction that stops moving price marks absorption and
the move turns. Long follows absorbed selling, short follows absorbed buying.
The strategy never trades with the absorbed flow.

What changed from the previous proposal, and why. The last trial (5m bars,
one-bar delta z-score, 35bp stop / 70bp target, up to three entries a session)
completed 1,892 trades over 960 sessions for a score of -0.2761. At 10bp of
round-trip cost that is roughly 1.9 notional-turns of pure fee, and the
per-trade expectancy was indistinguishable from zero: whatever edge the
one-bar reading has, the fee ate it. Two changes attack exactly that.

* The signal is now a WINDOW divergence, not a bar spike. Over the last
  `div_bars` = 36 five-minute bars (three hours) the volume-weighted flow is
  `imb = sum(delta) / sum(buy+sell volume)`, and the price change over the same
  window is `ret`. Absorption is `|imb|` large in one direction while `ret` has
  not followed it: net sellers of 18% of three hours of volume, and price flat
  or up. That is the claim stated once, over a horizon long enough that the
  bar-to-bar noise of a single 5m delta z-score is averaged out. It is also a
  genuinely different measurement from the previous trial rather than a
  re-parameterisation of it.

* One entry, held for hours, instead of a tight bracket churned three times a
  day. The bracket is now tail protection -- 4 ATR stop, 8 ATR target, where
  ATR is the mean 5m range over the last two hours -- and the binding exit is a
  time stop at `max_hold_bars` = 48 bars (four hours) plus the forced flat
  before the UTC close. Both trailing numbers are a deliberate trade: a 0.10%
  round trip can only be paid out of a move of that order, so the position has
  to be given the hours in which such a move can occur rather than a bar or two.
  Entries are capped at two a session with a three-hour cooldown between them,
  which roughly halves the turnover of the previous trial while keeping the
  exposure floor satisfied -- the raw divergence fires several times a session,
  so a session essentially always gets at least one entry.

Choices the brief leaves open, held fixed:

* Prior move -- implicit in the divergence. `imb` and `ret` over the same
  window are the move and the failure of the move, measured together; a
  separate prior-move threshold was what made the 15m trial fire on 5 sessions
  in 960 and go unscored. Nothing here retests an extreme level, which is the
  rule the last trial used and which is the reason it entered late in the
  absorption bar's own range.
* "Unusually large" delta -- normalised by total traded volume in the window,
  not by raw contracts. BTC volume varies by orders of magnitude across hours
  and years, so a contract-count z-score is mostly a clock.
* "Fails to extend" -- the current bar must still carry flow in the absorbed
  direction (`delta` of the same sign as `imb`) and must close in the upper
  (lower) 40% of its own range. Heavy selling that closes on its low is a
  breakdown, not an absorption.
* Confirmation -- none beyond that bar. Entry is at market on the close of the
  bar the divergence is read on, so it is eligible on the next event (T+1).
  Resting limits were considered and rejected: fills here are book-queue
  pessimistic, so a resting bid behind displayed size fills preferentially in
  the states where the reversal failed, which is adverse selection on entry
  that costs more than the touch.
* No reversals -- a new trade opens only from flat with nothing working, so
  long -> short is always expressed as long -> flat -> short.
* Costs and funding -- the bracket is given as offsets from the actual fill,
  never as absolute levels, so a slipped fill cannot invert it. Positions are
  flat before the UTC close by construction (`emit_empty=True` guarantees a
  callback to flatten against even in a dead tape), so no trade spans a
  funding stamp that is not inside its own session.

Drawdown control is deliberately loose: a stop tight enough to matter over four
hours would truncate the very drift the claim asserts. 4 ATR is roughly 0.5% at
typical BTC 5m ranges -- large enough to be a tail stop, small enough that a
single bad trade cannot dominate the run.

Everything reads forward-only. No `ctx.history()` call is made at all: the bars
the strategy needs are accumulated on `self` in bounded deques with incremental
sums as they close, so nothing is recomputed per bar, nothing grows across the
960 sessions, and there is no window to overrun.
"""

from __future__ import annotations

from collections import deque

NS_PER_SECOND = 1_000_000_000


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        size: int = 10,
        div_bars: int = 36,
        atr_bars: int = 24,
        imb_thr: float = 0.18,
        flat_frac: float = 0.0015,
        reclaim_frac: float = 0.40,
        stop_atr: float = 4.0,
        target_atr: float = 8.0,
        max_hold_bars: int = 48,
        max_entries: int = 2,
        cooldown_bars: int = 36,
        min_left_s: int = 7200,
        flat_buffer_s: int = 600,
    ) -> None:
        self.interval = interval
        self.size = size
        self.div_bars = div_bars
        self.atr_bars = atr_bars
        self.imb_thr = imb_thr
        self.flat_frac = flat_frac
        self.reclaim_frac = reclaim_frac
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.max_hold_bars = max_hold_bars
        self.max_entries = max_entries
        self.cooldown_bars = cooldown_bars
        self.min_left_ns = min_left_s * NS_PER_SECOND
        self.flat_buffer_ns = flat_buffer_s * NS_PER_SECOND

        # Bounded incremental feature state. Nothing here grows over the run.
        self.d_delta: deque = deque(maxlen=div_bars)
        self.d_vol: deque = deque(maxlen=div_bars)
        self.d_close: deque = deque(maxlen=div_bars + 1)
        self.ranges: deque = deque(maxlen=atr_bars)
        self._d_sum = 0.0
        self._v_sum = 0.0
        self._r_sum = 0.0

        self._bar = 0
        self._last_entry_bar = -10 ** 9
        self._held = 0
        self._session = None
        self._entries = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        # emit_empty so the end-of-session flatten always has a callback.
        ctx.subscribe(self.interval, emit_empty=True)

    # --------------------------------------------------------------- features

    def _update(self, bar) -> None:
        delta = float(bar.delta)
        vol = float(bar.buy_volume + bar.sell_volume)
        rng = float(bar.high - bar.low)

        if len(self.d_delta) == self.d_delta.maxlen:
            self._d_sum -= self.d_delta[0]
            self._v_sum -= self.d_vol[0]
        self.d_delta.append(delta)
        self.d_vol.append(vol)
        self._d_sum += delta
        self._v_sum += vol

        if len(self.ranges) == self.ranges.maxlen:
            self._r_sum -= self.ranges[0]
        self.ranges.append(rng)
        self._r_sum += rng

        self.d_close.append(float(bar.close))

    def _atr(self) -> float:
        if len(self.ranges) < self.atr_bars:
            return 0.0
        return self._r_sum / len(self.ranges)

    # ----------------------------------------------------------------- signal

    def _signal(self, bar) -> int:
        """+1 absorbed selling (price held despite net selling), -1 the mirror."""
        if len(self.d_delta) < self.div_bars:
            return 0
        if len(self.d_close) < self.div_bars + 1:
            return 0
        if len(self.ranges) < self.atr_bars:
            return 0
        if self._v_sum <= 0.0:
            return 0

        start = self.d_close[0]
        if start <= 0.0:
            return 0
        ret = (self.d_close[-1] - start) / start
        imb = self._d_sum / self._v_sum

        rng = float(bar.high - bar.low)
        vol = float(bar.buy_volume + bar.sell_volume)
        if rng <= 0.0 or vol <= 0.0:
            return 0
        dr = float(bar.delta) / vol

        # Long: heavy net selling over the window, price flat or up over it,
        # the current bar still selling and closing off its own low.
        if imb <= -self.imb_thr and ret >= -self.flat_frac:
            if dr > 0.0:
                return 0
            if (float(bar.close) - float(bar.low)) < self.reclaim_frac * rng:
                return 0
            return 1

        # Short: heavy net buying, price flat or down over it, the current bar
        # still buying and closing off its own high.
        if imb >= self.imb_thr and ret <= self.flat_frac:
            if dr < 0.0:
                return 0
            if (float(bar.high) - float(bar.close)) < self.reclaim_frac * rng:
                return 0
            return -1

        return 0

    # ------------------------------------------------------------------- walk

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            # A UTC boundary is a calendar convention on a market with no close
            # and no gap, so the flow statistics carry over untouched; only the
            # per-session entry budget and the hold clock reset.
            self._session = ctx.session
            self._entries = 0
            self._held = 0

        empty = bar.tick_count <= 0
        if not empty:
            self._bar += 1
            self._update(bar)

        if not ctx.actionable:
            return

        left_ns = ctx.session_end - ctx.ts

        # ---- carrying a trade: the bracket, the hold clock and the session
        #      clock are the only exits. Never open the opposite side here.
        if ctx.position != 0:
            self._held += 1
            if self._held >= self.max_hold_bars or left_ns <= self.flat_buffer_ns:
                self._flatten(ctx)
            return
        self._held = 0

        if empty:
            return
        if ctx.working_order_ids:
            return
        if left_ns <= self.min_left_ns:
            return
        if self._entries >= self.max_entries:
            return
        if self._bar - self._last_entry_bar < self.cooldown_bars:
            return

        signal = self._signal(bar)
        if signal == 0:
            return

        atr = self._atr()
        if atr <= 0.0:
            return
        stop = self.stop_atr * atr
        target = self.target_atr * atr
        if stop <= 0.0 or target <= 0.0:
            return

        if signal > 0:
            oid = ctx.buy(size=self.size, stop_offset=stop,
                          target_offset=target, tag="absorb-entry")
        else:
            oid = ctx.sell(size=self.size, stop_offset=stop,
                           target_offset=target, tag="absorb-entry")
        if oid is None:
            return
        self._entries += 1
        self._last_entry_bar = self._bar
        self._held = 0

    # ------------------------------------------------------------------ exits

    def _flatten(self, ctx) -> None:
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position != 0:
            ctx.close()

    # -------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
