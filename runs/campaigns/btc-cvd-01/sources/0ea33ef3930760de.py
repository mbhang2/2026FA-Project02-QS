"""Cumulative volume delta continuation on the BTC perpetual.

The claim under test: aggressive buying (selling) on the BTC perp pushes price
up (down) for a while afterwards, because a participant who needs more than the
displayed liquidity keeps trading and each trade moves the price.

How the claim is turned into something measurable, and why these choices:

**Measuring "large".** BTC volume swings by an order of magnitude across hours
and years, so a fixed delta is meaningless. Each bar carries `bar.delta`
(buy_volume - sell_volume, unknown aggressor excluded, and on this feed there is
none) and `bar.volume`, so the natural scale-free statistic is

    imbalance = sum(delta over the last K bars) / sum(volume over the last K bars)

which lives in [-1, 1] and says "of everything that traded in this window, this
fraction was net aggressive buying". It is computed from bars, not ticks: a tick
subscription costs roughly 5x and buys nothing here, since the bar builder
already accumulates both sides per bar.

Fixed thresholds still mean different things in a quiet week and a violent one,
so "large" is defined *against the strategy's own recent past*: the current
imbalance must sit in the extreme tail of the last `hist_len` imbalance
readings. That is a rolling quantile kept on `self` -- the engine's history
window is bounded at 512 bars and a percentile needs a deque, not `history()`.
`min_abs_imbalance` is a floor under the quantile so that a dead-flat stretch
cannot make a 2% imbalance look extreme.

**The horizon, named and held.** K = 8 bars of 30m = 4 hours of flow, and the
position is held for 8 bars = the next 4 hours. That is the claim stated as a
sentence: four hours of one-sided aggression predicts the next four hours. The
exit is a time exit, not a target, because a target would silently change the
horizon being tested on exactly the bars that moved most.

**Costs are the whole game.** Commission is 5bp per side, 10bp round trip, plus
the spread the pessimistic fill model charges on the way in and out. A 4-hour
BTC move is on the order of 50-100bp of standard deviation, so a signal that
fires constantly is dead on arrival: 300 round trips is 30% of notional in fees.
Hence the 97th percentile tail rather than a loose threshold, one position at a
time, a cooldown after each exit, and no pyramiding. The strategy should trade
a few times a week, not a few times a day.

**Entry and exit order types.** Both are market orders, deliberately. A resting
limit entry would be filled only when a trade goes strictly through it, which
under this fill model means the fill is conditional on the price moving against
the position first -- a selection effect that would contaminate a test of a
directional claim. Paying the spread for an unconditional entry is the honest
way to measure whether the drift exists.

**Funding.** Positions held across 00:00/08:00/16:00 UTC pay or receive the
funding rate. A 4-hour hold straddles at most one settlement and the rate is
small next to the 10bp round-trip fee, so it is not modelled; it is stated here
because it is a real, uncompensated transfer and it makes the long side (which
usually pays) slightly worse than measured.

**Risk.** There is no bracket. A bracket's target would cut the horizon short
exactly when the signal worked, which is the thing being measured. In its place
is a hard stop checked on every bar close: if the position is 2.5% against its
average entry, it is flattened. That bounds a flash move without turning the
trade into a different experiment. Position size is one lot (0.0001 BTC), held
one at a time, so the exposure is small and constant.

Warmup bars are accumulated but not traded: the imbalance history must be ready
at the first actionable bar, and `ctx.actionable` is what separates the two.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade in the direction of unusually one-sided aggressive flow."""

    def __init__(
        self,
        interval: str = "30m",
        flow_window: int = 8,          # bars of flow summed: 4 hours
        hold_bars: int = 8,            # bars held: the next 4 hours
        hist_len: int = 240,           # imbalance readings kept: ~5 days
        min_samples: int = 96,         # ~2 days before a percentile means much
        quantile: float = 0.97,        # tail that counts as "large"
        min_abs_imbalance: float = 0.05,
        cooldown: int = 4,             # bars flat before another entry
        hard_stop: float = 0.025,      # 2.5% adverse move flattens
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.flow_window = flow_window
        self.hold_bars = hold_bars
        self.hist_len = hist_len
        self.min_samples = min_samples
        self.quantile = quantile
        self.min_abs_imbalance = min_abs_imbalance
        self.cooldown = cooldown
        self.hard_stop = hard_stop
        self.size = size

        # Rolling state only. Nothing is recomputed over a long history.
        self._deltas: deque = deque(maxlen=flow_window)
        self._volumes: deque = deque(maxlen=flow_window)
        self._imbalances: deque = deque(maxlen=hist_len)
        self._held = 0
        self._cool = 0

    def setup(self, ctx) -> None:
        # One subscription. Bars carry delta and both volume sides, so no tick
        # subscription is needed for an order-flow feature at this resolution.
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Several bars can close at one instant; act only on ours.
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # Accumulate on every bar, warmup included, so the percentile is ready
        # at the first actionable bar.
        self._deltas.append(bar.delta)
        self._volumes.append(bar.volume)
        if len(self._deltas) < self.flow_window:
            return
        volume = sum(self._volumes)
        if volume <= 0:
            return
        imbalance = sum(self._deltas) / volume
        self._imbalances.append(imbalance)

        if not ctx.actionable:
            return

        if ctx.position != 0:
            self._manage_open_position(ctx)
            return

        # Flat. An entry already in flight blocks a second one.
        if ctx.working_order_ids:
            return
        if self._cool > 0:
            self._cool -= 1
            return
        if len(self._imbalances) < self.min_samples:
            return

        # The tail of what this strategy has seen, not a number from outside.
        magnitude = sorted(abs(x) for x in self._imbalances)
        threshold = magnitude[int(self.quantile * (len(magnitude) - 1))]
        if threshold < self.min_abs_imbalance:
            threshold = self.min_abs_imbalance

        if imbalance > threshold:
            self._held = 0
            ctx.buy(size=self.size, tag="cvd-long")
        elif imbalance < -threshold:
            self._held = 0
            ctx.sell(size=self.size, tag="cvd-short")

    def _manage_open_position(self, ctx) -> None:
        entry = ctx.average_entry_price
        last = ctx.last_price
        if entry > 0 and last > 0:
            move = (last - entry) / entry
            if (ctx.position > 0 and move <= -self.hard_stop) or (
                ctx.position < 0 and move >= self.hard_stop
            ):
                ctx.close()
                self._held = 0
                self._cool = self.cooldown
                return

        self._held += 1
        if self._held >= self.hold_bars:
            ctx.close()
            self._held = 0
            self._cool = self.cooldown

    def on_fill(self, ctx) -> None:
        # The clock starts on the bar after the fill; nothing else to arm.
        if ctx.fill.position == 0:
            self._held = 0

    def on_lifecycle(self, ctx) -> None:
        # No order ids are kept across callbacks, so there is nothing to clean
        # up when an order expires or is rejected.
        pass
