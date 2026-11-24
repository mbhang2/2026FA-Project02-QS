"""Absorption reversal on the OKX BTCUSDT perpetual.

Claim under test (campaign question, 2026-11-22): after a directional move,
unusually large aggressive flow in that direction that fails to move price marks
absorption, and price tends to turn. Long follows absorbed selling after a fall;
short follows absorbed buying after a rise. The strategy never trades with the
absorbed flow.

Every degree of freedom the question leaves open, decided and held fixed:

Prior move. Over the last `move_bars` closed bars, measured on closes: the
close-to-close change must exceed `min_move_atr` average bar ranges in the
direction that is about to be absorbed. At 15m and 8 bars that is a two-hour
move.

Unusually large delta. `bar.delta` normalised against the last `norm_bars`
deltas as a z-score (mean and variance kept incrementally on `self`, so no bar
re-walks the window), and additionally required to be at least
`delta_vol_frac` of the bar's own traded volume, which stops a high z-score in a
dead volume regime from counting as aggressive flow.

Fails to extend. The bar must sweep the prior `sweep_bars` extreme in the
direction of the flow, then close back off it by at least `reclaim_frac` of its
own range. A bar that makes a new low and closes in the upper part of itself,
on outsized sell delta, is selling absorbed into resting bids. The mirror for
highs. This is the "close back inside the range" rule referenced to the bar's
own extreme, because that is measurable within one bar and cannot be revised
afterwards.

Entry. Market on the close of the absorption bar, so eligible on the next event
(T+1). Resting limits were considered and rejected: fills here are
book-queue-pessimistic, a resting bid sits behind displayed size, and a limit
placed after a reversal bar fills preferentially in the states where the
reversal failed. Adverse selection on entry is a worse cost than the touch.

Exit. Bracket given as offsets, so it is validated against the actual fill and
cannot invert: `stop_atr` and `target_atr` average ranges. Plus a time exit flat
by `hold_margin_s` before the session close, because each trade must open and
close inside one UTC day.

Flat-to-flat. One position at a time. A signal opposing an open position is
dropped, never reversed; the bracket or the time stop closes the current trade
first, and a new entry is a separate trade.

Costs. 10bp per round trip. That is why the interval is 15m and the targets are
multiples of the bar range rather than fixed ticks: turnover, not signal
detection, is what kills this family. A 0.10% hurdle at BTC prices needs a swing
of a few tenths of a percent to be worth taking, and 15m bars are the smallest
horizon where a 2-4 ATR bracket reliably spans it. Funding at 08:00 and 16:00
UTC can be paid on a position held across them; at typical perpetual rates that
is a fraction of the round-trip cost, and forcing an exit into a funding stamp
would cost more than it saves, so it is accepted and stated rather than dodged.

Robustness. Feature buffers are bounded deques with incremental sums; nothing
grows across the 960 sessions and nothing recomputes history per bar. The
statistics deliberately persist across the UTC session boundary: this market has
no close and no gap, so a boundary is a calendar convention, and clearing a
24-hour delta window every midnight would leave the first third of every day
untradable. Only the per-session trade counter resets.
"""

from __future__ import annotations

from collections import deque

NS_PER_SECOND = 1_000_000_000


class Strategy:
    def __init__(
        self,
        interval: str = "15m",
        move_bars: int = 8,
        sweep_bars: int = 8,
        norm_bars: int = 64,
        atr_bars: int = 24,
        delta_z: float = 1.5,
        delta_vol_frac: float = 0.20,
        min_move_atr: float = 1.0,
        reclaim_frac: float = 0.40,
        stop_atr: float = 2.0,
        target_atr: float = 3.0,
        hold_margin_s: int = 1200,
        size: int = 1,
        max_trades_per_session: int = 4,
    ) -> None:
        self.interval = interval
        self.move_bars = move_bars
        self.sweep_bars = sweep_bars
        self.norm_bars = norm_bars
        self.atr_bars = atr_bars
        self.delta_z = delta_z
        self.delta_vol_frac = delta_vol_frac
        self.min_move_atr = min_move_atr
        self.reclaim_frac = reclaim_frac
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.hold_margin_s = hold_margin_s
        self.size = size
        self.max_trades_per_session = max_trades_per_session

        # Bounded incremental state; nothing here grows over the run.
        self.closes: deque = deque(maxlen=256)
        self.highs: deque = deque(maxlen=64)
        self.lows: deque = deque(maxlen=64)
        self.deltas: deque = deque(maxlen=norm_bars)
        self.vols: deque = deque(maxlen=norm_bars)
        self.ranges: deque = deque(maxlen=atr_bars)

        self._d_sum = 0.0
        self._d_sq = 0.0
        self._v_sum = 0.0
        self._r_sum = 0.0

        self.last_delta = 0.0
        self.last_volume = 0.0
        self.last_range = 0.0

        self._session = None
        self._trades_this_session = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # ---------------------------------------------------------------- helpers

    def _push(self, dq: deque, val: float, name: str, sq: str) -> None:
        """Append to a bounded deque, maintaining running sum (and sum of squares)."""
        if dq.maxlen is not None and len(dq) == dq.maxlen:
            old = dq[0]
            setattr(self, name, getattr(self, name) - old)
            if sq:
                setattr(self, sq, getattr(self, sq) - old * old)
        dq.append(val)
        setattr(self, name, getattr(self, name) + val)
        if sq:
            setattr(self, sq, getattr(self, sq) + val * val)

    def _flat_soon(self, ctx) -> bool:
        return (ctx.session_end - ctx.ts) <= self.hold_margin_s * NS_PER_SECOND

    def _update_features(self, bar) -> None:
        delta = float(bar.delta)
        volume = float(bar.buy_volume + bar.sell_volume)
        rng = float(bar.high - bar.low)

        self._push(self.deltas, delta, "_d_sum", "_d_sq")
        self._push(self.vols, volume, "_v_sum", "")
        self._push(self.ranges, rng, "_r_sum", "")

        self.closes.append(float(bar.close))
        self.highs.append(float(bar.high))
        self.lows.append(float(bar.low))

        self.last_delta = delta
        self.last_volume = volume
        self.last_range = rng

    def _signal(self):
        """+1 absorbed selling after a fall, -1 absorbed buying after a rise."""
        if len(self.deltas) < self.norm_bars:
            return None
        if len(self.closes) < self.move_bars + 1:
            return None
        if len(self.lows) < self.sweep_bars + 2:
            return None
        if len(self.ranges) < self.atr_bars:
            return None

        atr = self._r_sum / len(self.ranges)
        if atr <= 0.0:
            return None

        n = len(self.deltas)
        mean = self._d_sum / n
        var = self._d_sq / n - mean * mean
        if var <= 0.0:
            return None
        sd = var ** 0.5
        z = (self.last_delta - mean) / sd

        if self.last_volume <= 0.0:
            return None
        if abs(self.last_delta) < self.delta_vol_frac * self.last_volume:
            return None

        rng = self.last_range
        if rng <= 0.0:
            return None

        close = self.closes[-1]
        move = close - self.closes[-self.move_bars - 1]
        min_move = self.min_move_atr * atr
        if min_move <= 0.0:
            return None

        prior_lows = tuple(self.lows)[-(self.sweep_bars + 1):-1]
        prior_highs = tuple(self.highs)[-(self.sweep_bars + 1):-1]
        if not prior_lows or not prior_highs:
            return None

        if z <= -self.delta_z and move <= -min_move:
            swept = self.lows[-1] < min(prior_lows)
            reclaimed = (close - self.lows[-1]) >= self.reclaim_frac * rng
            if swept and reclaimed:
                return 1
            return None

        if z >= self.delta_z and move >= min_move:
            swept = self.highs[-1] > max(prior_highs)
            given_back = (self.highs[-1] - close) >= self.reclaim_frac * rng
            if swept and given_back:
                return -1
            return None

        return None

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            # New UTC day: reset the trade counter only. The flow statistics
            # deliberately carry over, because there is no close and no gap.
            self._session = ctx.session
            self._trades_this_session = 0

        if bar.tick_count == 0:
            return
        self._update_features(bar)

        if not ctx.actionable:
            return

        # Exits before entries, always.
        if ctx.position:
            if self._flat_soon(ctx):
                ctx.close(tag="time-exit")
            return

        if ctx.working_order_ids:
            return
        if self._trades_this_session >= self.max_trades_per_session:
            return
        if self._flat_soon(ctx):
            return

        signal = self._signal()
        if signal is None:
            return

        atr = self._r_sum / len(self.ranges) if self.ranges else 0.0
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
        self._trades_this_session += 1

    # -------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
