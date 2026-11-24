"""Cumulative volume delta continuation on the OKX BTCUSDT perpetual.

The claim this file tests, and nothing else
-------------------------------------------
The registered question (2026-11-22) is that unusually large *aggressive* flow
is followed by continuation in the same direction over the next comparable
window. `bar.delta` is aggressor-signed volume -- `buy_volume - sell_volume`,
built by the engine from the trade tape -- so a running sum of it over a window
is cumulative volume delta. This strategy goes long after unusually positive
CVD and short after unusually negative CVD. It does not fade, it does not use
the book (which it cannot see anyway), and it does not try to classify regimes.

How "large" is defined, which is the choice the question leaves open
--------------------------------------------------------------------
Two normalisations, because BTC volume moves by an order of magnitude across
hours and years and a fixed contract count would mean "large" at 03:00 and
"small" at 14:00.

1. Scale by traded volume. Over a twelve-hour window (twelve one-hour bars)
   accumulate delta and total volume separately and take the ratio
   `imbalance = sum(delta) / sum(volume)`. This is bounded in [-1, 1] and is
   directly "what fraction of the last twelve hours of trade was aggressive
   buying net of aggressive selling".

2. Scale by its own recent past. The twelve-hour imbalance is z-scored against
   its trailing 48 hourly values, so a period whose flow is structurally wider
   needs a bigger absolute deviation to fire. A second, absolute gate
   (`|imbalance| >= min_imbalance`) exists because z is a ratio: on a dead tape
   the trailing dispersion collapses and z can reach 2 on nothing at all. The
   denominator going to zero is the standard way a z-score gate stops meaning
   anything, so it is gated as well as floored.

Horizon, entry, exit
--------------------
Twelve hours, stated and held: the window that measures the flow and the window
that holds the position are the same length, which is the literal form of "the
next comparable window". Entry is a market order -- size is one lot of 0.0001
BTC, orders of magnitude inside the top of book, so it pays the touch rather
than walking levels. Exit is a market order twelve bars later. A 4% adverse
move from the average entry closes early, but that is a backstop and not the
strategy: BTC's own twelve-hour sigma is on the order of 1.5%, so this fires
only on a genuine dislocation, and the fixed horizon is what actually ends the
trade.

Costs, and why the horizon is hours rather than minutes
-------------------------------------------------------
Five basis points per side is ten basis points round trip, charged on every
fill. A one-hour signal traded every hour pays that dozens of times a day and
cannot survive whatever its raw predictive power; a twelve-hour signal traded
roughly once a day pays it once. That arithmetic, not any estimate of edge, is
what sets the horizon here. The gate is deliberately strict (two standard
deviations plus an absolute floor of five percent net flow over half a day),
because the only way a flow effect of a few basis points per trade is visible
after costs is to trade it rarely.

Funding at 00:00, 08:00 and 16:00 UTC is charged on a held position. Its size
is not quoted anywhere this strategy can read, and at roughly a basis point per
settlement against a ten basis point round trip and a multi-hour hold it is
second order; no attempt is made to dodge it, because a strategy that dodged it
would no longer be holding the horizon it says it is testing.

State
-----
Only three bounded lists, appended to as bars arrive: the last `flow_window`
bar deltas, the matching volumes, and the last `baseline_window + 1` imbalance
values. No callback reads `ctx.history(...)`, so the pushed window is irrelevant
here and nothing grows across sessions.
"""

from __future__ import annotations


class Strategy:
    """Buy unusually positive aggressive flow, sell unusually negative flow."""

    def __init__(
        self,
        interval: str = "1h",
        flow_window: int = 12,
        baseline_window: int = 48,
        entry_z: float = 2.0,
        min_imbalance: float = 0.05,
        hold_bars: int = 12,
        stop_pct: float = 0.04,
        cooldown_bars: int = 2,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.flow_window = int(flow_window)
        self.baseline_window = int(baseline_window)
        self.entry_z = float(entry_z)
        self.min_imbalance = float(min_imbalance)
        self.hold_bars = int(hold_bars)
        self.stop_pct = float(stop_pct)
        self.cooldown_bars = int(cooldown_bars)
        self.size = int(size)

        # Bounded accumulators; the signal is a function of these only.
        self._deltas: list[float] = []
        self._volumes: list[float] = []
        self._imbalances: list[float] = []

        self._held = 0
        self._cooldown = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # With one subscription this is always true, but asking the question
        # explicitly means a bar can never be counted twice.
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # --- accumulate aggressive flow ------------------------------------
        # delta excludes unknown-aggressor volume by construction. On this
        # feed every trade carries its side, so nothing is being silently
        # dropped here.
        delta = float(bar.buy_volume) - float(bar.sell_volume)
        volume = float(bar.volume)
        self._deltas.append(delta)
        self._volumes.append(volume)
        if len(self._deltas) > self.flow_window:
            del self._deltas[0]
            del self._volumes[0]

        # --- manage an open position before anything else -------------------
        if ctx.position != 0:
            self._held += 1
            if ctx.working_order_ids:
                return  # an exit is already in flight; do not stack another
            entry_price = ctx.average_entry_price
            exit_now = self._held >= self.hold_bars
            if not exit_now and entry_price and entry_price > 0.0:
                if ctx.position > 0:
                    move = (bar.close - entry_price) / entry_price
                else:
                    move = (entry_price - bar.close) / entry_price
                if move <= -self.stop_pct:
                    exit_now = True
            if exit_now:
                ctx.close()
                self._held = 0
                self._cooldown = self.cooldown_bars
            return

        # Flat.
        self._held = 0
        if self._cooldown > 0:
            self._cooldown -= 1
            return

        if len(self._deltas) < self.flow_window:
            return
        if not ctx.actionable or ctx.working_order_ids:
            return

        total_volume = 0.0
        for value in self._volumes:
            total_volume += value
        if total_volume <= 0.0:
            return
        total_delta = 0.0
        for value in self._deltas:
            total_delta += value
        imbalance = total_delta / total_volume

        self._imbalances.append(imbalance)
        if len(self._imbalances) > self.baseline_window + 1:
            del self._imbalances[0]

        # --- is this imbalance large relative to its own recent past? -------
        baseline = self._imbalances[:-1]
        if len(baseline) < 24:
            return
        mean = 0.0
        for value in baseline:
            mean += value
        mean /= len(baseline)
        variance = 0.0
        for value in baseline:
            diff = value - mean
            variance += diff * diff
        variance /= len(baseline)
        if variance <= 0.0:
            return
        sd = variance ** 0.5
        if sd <= 1e-9:
            return
        z = (imbalance - mean) / sd

        # Absolute gate: a z-score on a collapsed dispersion is noise.
        if abs(imbalance) < self.min_imbalance:
            return

        if z >= self.entry_z:
            ctx.buy(size=self.size, tag="cvd-long")
        elif z <= -self.entry_z:
            ctx.sell(size=self.size, tag="cvd-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
