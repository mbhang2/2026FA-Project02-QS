"""Cumulative volume delta continuation on the OKX BTCUSDT perpetual.

THE CLAIM, AND WHAT THIS FILE DOES ABOUT IT
-------------------------------------------
The campaign question is the aggressive-flow continuation hypothesis: when buy
volume initiated by aggressors outweighs sell volume over some window, price
keeps rising for a while; when aggressive selling outweighs buying, price keeps
falling. This file tests exactly that direction -- it follows the flow, it never
fades it. If flow continuation is not there, this strategy should come out flat
to negative, and that is the honest answer rather than a rotation to the other
side.

THE MEASUREMENT
---------------
Every 5m bar carries `buy_volume` and `sell_volume` accumulated tick by tick by
the bar builder, so `bar.delta` is signed order flow and no tick subscription is
needed (a tick subscription is ~5x the wall-clock cost here and adds nothing to
a bar-resolution feature). Over the last `flow_bars` = 12 bars (one hour) I sum
delta and sum volume and take the ratio

    imbalance = sum(delta) / sum(volume)

which is bounded in [-1, 1] and already volume-normalised: BTC volume differs
widely across hours and years, and dividing by the volume of the same window is
the cheapest honest normalisation there is.

"Unusually large" is then a z-score against the *strategy's own* recent history
of that imbalance: a rolling 2016-bar (seven-session) mean and standard deviation
kept with running sums in O(1) per bar. That adapts to a regime where the whole
flow distribution shifts (a persistent bid in a bull market) without ever
demeaning away the level, because the level only sets the baseline, not the
signal. An absolute floor on |imbalance| stops a dead-calm window from producing
a huge z out of a rounding-sized flow.

HORIZON, ENTRY AND EXIT
-----------------------
The horizon under test is four hours (`hold_bars` = 48). Entries are **market**
orders because the fill model here is book-queue-pessimistic: a resting limit
only fills when a trade strictly goes through it, i.e. only when the price has
already moved against the position, so a limit entry would systematically buy
the flow signal's losers. Market orders pay the touch and are unambiguous.

The exit is a **time exit**: the claim is about the next comparable window, so
the position is held for exactly that window and then flattened with a market
order. A wide bracket (3% stop, 4% target) rides along as tail protection only --
at four-hour BTC volatility both legs are several sigma away and almost never
trigger, so the horizon stays fixed while a crash cannot run unstopped. Bracket
levels are given as **offsets**, never as absolute prices, so the 100 ms latency
between deciding and filling can never put a leg on the wrong side of the fill.

COSTS
-----
5 bp commission per side is 10 bp a round trip, so turnover is the enemy. Three
things bound it: a two-hour cooldown after any exit, one position at a time, and
a z threshold of 2.0 on a signal that is itself heavily autocorrelated so fires
in episodes rather than continuously. A guard also skips entries within fifteen
minutes of a funding settlement (00:00, 08:00, 16:00 UTC) so a fresh position is
not opened straight into a funding charge.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No book, no quotes, no multi-instrument, no regime model, no fade. Trades and
bars only. Every parameter has a default, no state is kept across sessions, and
nothing recomputes a window over the whole history on each callback.
"""

from __future__ import annotations

from collections import deque
from math import sqrt


class Strategy:
    """Follow unusually large aggressive flow over a fixed four-hour horizon."""

    def __init__(
        self,
        interval: str = "5m",
        flow_bars: int = 12,
        hold_bars: int = 48,
        cooldown_bars: int = 24,
        z_entry: float = 2.0,
        min_abs_imbalance: float = 0.02,
        norm_bars: int = 2016,
        min_norm_bars: int = 1008,
        size: int = 10,
        stop_frac: float = 0.03,
        target_frac: float = 0.04,
        funding_guard_min: int = 15,
    ) -> None:
        self.interval = interval
        self.flow_bars = int(flow_bars)
        self.hold_bars = int(hold_bars)
        self.cooldown_bars = int(cooldown_bars)
        self.z_entry = float(z_entry)
        self.min_abs_imbalance = float(min_abs_imbalance)
        self.norm_bars = int(norm_bars)
        self.min_norm_bars = int(min_norm_bars)
        self.size = int(size)
        self.stop_frac = float(stop_frac)
        self.target_frac = float(target_frac)
        self.funding_guard_min = int(funding_guard_min)

        # Raw flow over the measurement window.
        self.deltas: deque = deque(maxlen=self.flow_bars)
        self.volumes: deque = deque(maxlen=self.flow_bars)

        # Rolling distribution of the imbalance itself, kept in O(1) per bar
        # with running first and second moments over a fixed-length deque.
        self.imb_hist: deque = deque(maxlen=self.norm_bars)
        self._s1 = 0.0
        self._s2 = 0.0

        # Trade state.
        self.held = 0
        self.cooldown = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        # --- measurement, on every bar, warmup included ---------------------
        total_volume = 0.0
        total_delta = 0.0
        if len(self.deltas) == self.flow_bars:
            # Recompute from the deque: `flow_bars` is a dozen entries.
            total_delta = sum(self.deltas)
            total_volume = sum(self.volumes)

        self.deltas.append(float(bar.delta))
        self.volumes.append(float(bar.volume))
        total_delta += float(bar.delta)
        total_volume += float(bar.volume)

        if len(self.deltas) < self.flow_bars or total_volume <= 0.0:
            return

        imbalance = total_delta / total_volume

        # Rolling mean / variance of the imbalance, exact, amortised O(1).
        if len(self.imb_hist) == self.norm_bars:
            evicted = self.imb_hist[0]
            self._s1 -= evicted
            self._s2 -= evicted * evicted
        self.imb_hist.append(imbalance)
        self._s1 += imbalance
        self._s2 += imbalance * imbalance

        if not ctx.actionable:
            return

        # --- manage an open position ---------------------------------------
        if ctx.position != 0:
            self.held += 1
            if self.held >= self.hold_bars:
                # Cancel the protective bracket before flattening, so nothing
                # reduce-only is left resting against a book that is about to
                # be flat.
                ctx.cancel_all()
                ctx.close()
                self.held = 0
                self.cooldown = self.cooldown_bars
            return

        self.held = 0

        if self.cooldown > 0:
            self.cooldown -= 1
            return
        if ctx.working_order_ids:
            return

        # --- signal ---------------------------------------------------------
        n = len(self.imb_hist)
        if n < self.min_norm_bars:
            return
        mean = self._s1 / n
        var = self._s2 / n - mean * mean
        if var <= 1e-12:
            return
        std = sqrt(var)
        if std <= 1e-6:
            return
        if abs(imbalance) < self.min_abs_imbalance:
            return
        z = (imbalance - mean) / std
        if z >= self.z_entry:
            go_long = True
        elif z <= -self.z_entry:
            go_long = False
        else:
            return

        if self._near_funding(ctx):
            return

        price = ctx.last_price
        if price <= 0.0:
            return
        stop = price * self.stop_frac
        target = price * self.target_frac
        if stop <= 0.0 or target <= 0.0:
            return

        # Aggressive flow in the direction of the imbalance. Offsets, not
        # absolute levels: they are measured from the fill, so 100 ms of
        # slippage cannot invert a bracket leg.
        if go_long:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="cvd-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="cvd-short")

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx) -> None:
        # A non-zero position after the fill means the entry just landed: the
        # hold clock starts from the next bar, not from the decision bar.
        if ctx.fill.position != 0:
            self.held = 0

    # -------------------------------------------------------------- lifecycle

    def on_lifecycle(self, ctx) -> None:
        # Nothing to clean up: no order id is ever kept across callbacks, so a
        # DAY expiry or an OCO cancel cannot leave a stale id to act on.
        pass

    # ---------------------------------------------------------------- helpers

    def _near_funding(self, ctx) -> bool:
        """True inside the last few minutes before a funding settlement."""
        if self.funding_guard_min <= 0:
            return False
        try:
            now = ctx.now
        except Exception:  # pragma: no cover - the context always has one
            return False
        minutes = now.hour * 60 + now.minute
        into = minutes % 480  # funding at 00:00, 08:00 and 16:00 UTC
        return into >= 480 - self.funding_guard_min
