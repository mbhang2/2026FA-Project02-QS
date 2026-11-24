"""CVD continuation on the OKX BTC perpetual.

The claim under test: unusually large cumulative volume delta over a recent
window is followed by price continuation over a comparable forward window.
This file is an implementation of that claim, not of its opposite -- when the
flow is strongly one-sided it buys (or sells) in the direction of the flow and
holds.

Choices, and why each one is the one it is:

*Signal.* r = sum(bar.delta over the last L bars) / sum(bar.volume over the
last L bars). A ratio rather than a contract count, because BTC volume differs
by an order of magnitude across hours and across years and a fixed size would
mean "large" in 2021 and "noise" in 2026. r is then z-scored against a rolling
window of its own past values (maintained with running sums, so the whole thing
is O(1) per bar and nothing is recomputed over history). The z-score is the
"large" of the claim, and it adapts to the regime's own flow volatility rather
than to a threshold I picked off one month. An absolute floor on |r| is kept on
top of it: a statistically extreme hour in a dead market is still an
economically tiny one, and 10bp of round-trip cost does not care how many
standard deviations it was.

*Horizon.* L = 12 bars of 5m, one hour of flow, held for 12 bars, one hour.
The claim is about "the next comparable window", so the forward horizon is set
equal to the measurement window rather than tuned to the data. One hour is
also the shortest horizon at which the expected drift has a chance of being
larger than the 10bp round trip.

*Costs.* Five basis points a side is the binding constraint here, not the
signal. So the strategy is deliberately selective and deliberately capped:
z >= 2 plus the absolute floor, at most 3 round trips a session (0.3% of
notional a day in fees), and a re-arm rule so the same persistent imbalance is
not paid for repeatedly -- after a trade the strategy will not re-enter until
the imbalance has decayed back inside half a standard deviation. An effect that
pays before fees and not after them is not a tradable effect, and this design
makes that distinction visible in the blotter instead of hiding it.

*Entry.* Market. The effect is a drift over the following hour; being in for
the hour is the point. A resting limit would only fill when price came back
through it, which is precisely after the continuation being tested had failed.
Latency is 100ms against a one-hour horizon, so it is not the constraint.

*Exit.* Time, at the end of the holding window, plus a 2% adverse hard stop so
a single tail event cannot define the run. Deliberately no profit target: a
target would truncate exactly the continuation the claim asserts, turning the
test into something else.

*Position.* One lot, one position at a time, flat between trades. Sizing is not
part of the claim and is left at the reference scale.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 12,
        hold_bars: int = 12,
        ref_window: int = 1000,
        min_ref: int = 150,
        z_entry: float = 2.0,
        z_reset: float = 0.5,
        min_abs_r: float = 0.08,
        max_trades_per_session: int = 3,
        hard_stop: float = 0.02,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback = lookback
        self.hold_bars = hold_bars
        self.ref_window = ref_window
        self.min_ref = min_ref
        self.z_entry = z_entry
        self.z_reset = z_reset
        self.min_abs_r = min_abs_r
        self.max_trades_per_session = max_trades_per_session
        self.hard_stop = hard_stop
        self.size = size

        # Rolling window of the flow ratio's parts. Bounded, updated per bar.
        self._delta: deque[float] = deque(maxlen=lookback)
        self._vol: deque[float] = deque(maxlen=lookback)

        # Distribution of past r values, with running moments so the z-score
        # costs nothing per bar. Kept on self rather than read from
        # ctx.history(), which is a bounded window.
        self._r_hist: deque[float] = deque(maxlen=ref_window)
        self._r_sum: float = 0.0
        self._r_sumsq: float = 0.0

        self._session: int | None = None
        self._session_trades: int = 0
        self._bars_in_trade: int = 0
        self._armed: bool = True

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]
        r = self._update_flow(bar)

        if ctx.session != self._session:
            self._session = ctx.session
            self._session_trades = 0

        z = self._zscore(r) if r is not None else None
        if z is not None and abs(z) < self.z_reset:
            self._armed = True

        if not ctx.actionable:
            return

        if ctx.position != 0:
            self._manage(ctx)
            return

        # r is checked here explicitly, not inferred from z, so the abs() below
        # is on a float.
        if r is None or z is None or not self._armed:
            return
        if self._session_trades >= self.max_trades_per_session:
            return
        if ctx.working_order_ids:
            return
        if abs(r) < self.min_abs_r:
            return

        if z >= self.z_entry:
            order_id = ctx.buy(size=self.size, tag="entry")
        elif z <= -self.z_entry:
            order_id = ctx.sell(size=self.size, tag="entry")
        else:
            return

        if order_id is not None:
            self._session_trades += 1
            self._bars_in_trade = 0
            self._armed = False

    # ------------------------------------------------------------ exit logic

    def _manage(self, ctx) -> None:
        self._bars_in_trade += 1
        entry = ctx.average_entry_price
        if entry > 0.0:
            direction = 1.0 if ctx.position > 0 else -1.0
            ret = (ctx.last_price - entry) / entry * direction
        else:
            ret = 0.0

        if self._bars_in_trade < self.hold_bars and ret > -self.hard_stop:
            return
        if ctx.working_order_ids:
            return
        ctx.close()

    # --------------------------------------------------------------- features

    def _update_flow(self, bar) -> float | None:
        """Fold one closed bar in and return the current flow ratio, or None."""
        self._delta.append(float(bar.delta))
        self._vol.append(float(bar.volume))
        if len(self._vol) < self.lookback:
            return None
        total_volume = sum(self._vol)
        if total_volume <= 0.0:
            return None
        r = sum(self._delta) / total_volume

        # Roll the reference distribution: drop the value about to be evicted,
        # then add the new one, so the running moments stay exact.
        if len(self._r_hist) == self._r_hist.maxlen and len(self._r_hist) > 0:
            old = self._r_hist[0]
            self._r_sum -= old
            self._r_sumsq -= old * old
        self._r_hist.append(r)
        self._r_sum += r
        self._r_sumsq += r * r
        return r

    def _zscore(self, r: float) -> float | None:
        n = len(self._r_hist)
        if n < self.min_ref:
            return None
        mean = self._r_sum / n
        var = self._r_sumsq / n - mean * mean
        if var <= 0.0:
            return None
        sd = var ** 0.5
        if sd < 1e-12:
            return None
        return (r - mean) / sd

    # -------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        # Entry or exit, the holding clock restarts; the exit path only reads
        # it while a position is open.
        self._bars_in_trade = 0

    def on_lifecycle(self, ctx) -> None:
        pass
