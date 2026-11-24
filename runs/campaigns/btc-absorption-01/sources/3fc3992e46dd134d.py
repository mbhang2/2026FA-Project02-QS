"""Absorption reversal on the OKX BTC perpetual (960 scored UTC-day sessions).

The claim under test: after a directional move, aggressive flow in that
direction that *fails to extend price* marks a reversal rather than a
continuation. Long after a fall whose heavy selling could not print a new low;
short after a rise whose heavy buying could not print a new high.

Implementation, and the choices the brief leaves open:

* Prior move -- the last `move_window` (12) closed 5m bars, as a fractional
  change from the window's first close. Required to be at least
  `max(0.8 * sigma_bar * sqrt(move_window), 0.15%)`, so the threshold is
  volatility-scaled but never degenerates in a dead tape.
* "Unusually large delta" -- the bar's own-flow imbalance `delta / volume` is
  normalised against the mean and standard deviation of the same quantity over
  the preceding `flow_window` (48) bars. Ratio rather than raw contracts,
  because BTC volume swings by orders of magnitude across hours and years; a
  raw-delta z-score would be a clock. Floor of 5% imbalance so a z-score in a
  quiet tape cannot fire on nothing.
* "Fails to extend" -- the absorption bar's low must not undercut the lowest
  low of the prior 12 bars (mirror for highs). One rule, held fixed.
* Close back inside the range -- the bar must close in the upper (lower)
  `close_frac` = 30% of its own range. This is the second half of the
  absorption reading: the flow was heavy *and* the bar rejected its own
  extreme.
* Confirmation -- none. Entry is on the absorption bar itself, at market, on
  the next event. No extra bar of delay: waiting costs an entry price.
* Entry/exit -- market entry with an engine-armed bracket as distances from
  the fill (never absolute levels, which invert on a slipped fill). Stop 35bp,
  target 70bp: a 2:1 payoff that needs about 42% wins to clear the 10bp round
  trip. Distances are fractions of price, not constants, because BTC's price
  level spans the whole sample.
* Flat within the UTC day -- entries are refused with less than 2 hours left,
  and any open position is cancelled-and-closed with 15 minutes left. The 5m
  subscription uses `emit_empty=True` so there is always a callback to exit
  against, even in a dead tape.
* No reversals: the only path from long to short is long -> flat -> short,
  which the "act only when flat and nothing working" gate enforces.

Cost is the whole experiment. Stops and targets are wide enough that a trade
must be a real reversal, not a wobble, and the entry count is capped at three
a session so a noisy day cannot churn the account into fees.

Everything reads forward-only: history is closed bars, warmup is skipped via
`ctx.actionable`, and the window is bounded by `last(n)` with a length check
first, so nothing reaches past a 512-bar push window.
"""

from __future__ import annotations

import math


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        size: int = 10,
        flow_window: int = 48,
        move_window: int = 12,
        vol_window: int = 96,
        move_sigmas: float = 0.8,
        move_floor: float = 0.0015,
        delta_floor: float = 0.05,
        delta_sigmas: float = 0.75,
        close_frac: float = 0.30,
        stop_frac: float = 0.0035,
        target_frac: float = 0.0070,
        max_entries: int = 3,
        min_hold_ns: int = 2 * 3600 * 1_000_000_000,
        flat_buffer_ns: int = 15 * 60 * 1_000_000_000,
    ) -> None:
        self.interval = interval
        self.size = size
        self.flow_window = flow_window
        self.move_window = move_window
        self.vol_window = vol_window
        self.move_sigmas = move_sigmas
        self.move_floor = move_floor
        self.delta_floor = delta_floor
        self.delta_sigmas = delta_sigmas
        self.close_frac = close_frac
        self.stop_frac = stop_frac
        self.target_frac = target_frac
        self.max_entries = max_entries
        self.min_hold_ns = min_hold_ns
        self.flat_buffer_ns = flat_buffer_ns

        self._session = None
        self._entries = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        # emit_empty so the end-of-day flatten always has a callback to run in.
        ctx.subscribe(self.interval, emit_empty=True)

    # ------------------------------------------------------------------- walk

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._entries = 0

        if not ctx.actionable:
            return

        remaining = ctx.session_end - ctx.ts

        if ctx.position:
            # The bracket owns the exit; the clock is the only thing left.
            if remaining <= self.flat_buffer_ns:
                self._flatten(ctx)
            return

        if ctx.working_order_ids:
            return
        if remaining <= self.min_hold_ns:
            return
        if self._entries >= self.max_entries:
            return

        side = self._signal(ctx)
        if side == 0:
            return

        price = ctx.last_price
        if price <= 0.0:
            return

        stop = price * self.stop_frac
        target = price * self.target_frac
        if side > 0:
            oid = ctx.buy(size=self.size, stop_offset=stop,
                          target_offset=target, tag="entry")
        else:
            oid = ctx.sell(size=self.size, stop_offset=stop,
                           target_offset=target, tag="entry")
        if oid is not None:
            self._entries += 1

    # ----------------------------------------------------------------- signal

    def _signal(self, ctx) -> int:
        flow = self.flow_window
        move_w = self.move_window
        need = max(flow, move_w, self.vol_window) + 1

        history = ctx.history(self.interval)
        if len(history) < need:
            return 0
        bars = history.last(need)

        last = bars[-1]
        if last.tick_count <= 0:
            return 0
        rng = last.high - last.low
        if rng <= 0.0:
            return 0
        vol = last.volume
        if vol <= 0:
            return 0

        # The absorption bar's own flow imbalance, and which way it leans.
        dr = last.delta / vol
        if dr > -self.delta_floor and dr < self.delta_floor:
            return 0
        long_side = dr < 0.0

        # The prior move: cheap check first, before any window loop.
        start = bars[-1 - move_w]
        if start.close <= 0.0:
            return 0
        move = (last.close - start.close) / start.close
        if abs(move) < self.move_floor:
            return 0
        if (move < 0.0) != long_side:
            return 0

        # Normalise the imbalance against recent flow.
        total = 0.0
        total_sq = 0.0
        count = 0
        for bar in bars[-(flow + 1):-1]:
            v = bar.volume
            if v > 0:
                x = bar.delta / v
                total += x
                total_sq += x * x
                count += 1
        if count < 10:
            return 0
        mean = total / count
        var = total_sq / count - mean * mean
        if var <= 1e-12:
            return 0
        z = (dr - mean) / math.sqrt(var)
        if (z < 0.0) != long_side:
            return 0
        if abs(z) < self.delta_sigmas:
            return 0

        # Volatility scale for the move threshold.
        seg = bars[-(self.vol_window + 1):]
        prev = seg[0].close
        sr = 0.0
        sr2 = 0.0
        cn = 0
        for bar in seg[1:]:
            c = bar.close
            if prev > 0.0:
                r = (c - prev) / prev
                sr += r
                sr2 += r * r
                cn += 1
            prev = c
        if cn < 20:
            return 0
        mr = sr / cn
        vr = sr2 / cn - mr * mr
        if vr <= 0.0:
            return 0
        scale = math.sqrt(vr) * math.sqrt(move_w)
        threshold = self.move_sigmas * scale
        if threshold < self.move_floor:
            threshold = self.move_floor
        if abs(move) < threshold:
            return 0

        # Did the flow fail to extend price?
        prior = bars[-(move_w + 1):-1]
        if long_side:
            low = prior[0].low
            for bar in prior:
                if bar.low < low:
                    low = bar.low
            if last.low < low:
                return 0
            if (last.close - last.low) / rng < self.close_frac:
                return 0
            return 1

        high = prior[0].high
        for bar in prior:
            if bar.high > high:
                high = bar.high
        if last.high > high:
            return 0
        if (last.high - last.close) / rng < self.close_frac:
            return 0
        return -1

    # ------------------------------------------------------------------ exits

    def _flatten(self, ctx) -> None:
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position:
            ctx.close()

    # -------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
