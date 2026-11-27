"""Cumulative volume delta continuation on the BTC perpetual.

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling is followed by higher
prices for a while, and aggressive selling by lower prices. Each bar already
carries the ingredients -- `bar.buy_volume`, `bar.sell_volume` and the derived
`bar.delta` -- so no tick subscription is needed and the engine keeps its
event-driven walk.

How this file measures it
-------------------------
* Flow window: the last 8 closed 15m bars (two hours). Sum of `bar.delta` over
  the sum of `bar.volume` is a signed imbalance in [-1, 1]. Dividing by volume
  is the normalisation the brief asks for: BTC volume differs by an order of
  magnitude between 2023 and 2026 and between 03:00 and 15:00 UTC, and a fixed
  contract count would mean wildly different things across the sample.
* "Large" is relative to what the run has already seen, not to a constant. A
  rolling window of the last 480 imbalance observations (five days at 15m) is
  kept on `self` with running sums, and an entry needs the current imbalance to
  sit `z_entry` standard deviations above (below) that window's mean, with an
  absolute floor of 0.15 so a quiet, tightly-distributed stretch cannot make a
  meaningless imbalance look extreme. That is the adaptive threshold the brief
  asks for, and it costs O(1) per bar rather than recomputing anything.
* Confirmation: for a long the price two hours ago must be below the last
  close. Heavy aggressive buying that has *not* moved price is absorption, not
  continuation, and it is the opposite of what the claim asserts. The filter is
  a constructor flag so the effect can be measured cleanly by turning it off.

The horizon
-----------
The position is held for exactly 12 closed bars (three hours) and then closed
with a market order. That is the horizon being tested: the brief's "for a
while", long enough for a drift to accumulate, short enough that the entry's
information has not decayed. Exits are time-based, not bracket-based, because
the claim is a statement about drift over a window, not about a level.

Costs decide everything here
----------------------------
Five basis points a side, so a round trip costs 0.10% before spread: at
$60,000 that is $60 per round trip in price terms. This strategy therefore
trades rarely on purpose -- a `cooldown` of eight bars after any exit, the
threshold above, and a three-hour minimum hold mean roughly one trade every
one to three days rather than one an hour. An effect that pays before fees and
not after them is not a tradable effect here, so the selectivity is the
strategy, not a refinement of it.

Execution
---------
Entries and exits are market orders. The fill model is queue-pessimistic -- a
resting limit fills only when trades go strictly through it -- so a momentum
entry as a resting bid would be filled precisely when the move has failed. The
spread paid is the price of participating at all.

Known limits, stated rather than buried
---------------------------------------
Crossing a funding settlement (00:00, 08:00, 16:00 UTC) pays or receives
funding; at typical rates that is a basis point against a ten-basis-point round
trip, so it is accepted rather than traded around. Positions are not flattened
at the session boundary -- a UTC day is a calendar convention on this market,
not a close, and the claim says nothing about midnight. The strategy is flat
unless a signal fires, so the exposure is the horizon, and only the horizon.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """CVD continuation: follow unusually one-sided aggressive flow."""

    def __init__(
        self,
        interval: str = "15m",
        lookback: int = 8,
        horizon: int = 12,
        z_entry: float = 2.2,
        min_ratio: float = 0.15,
        cooldown: int = 8,
        stats_len: int = 480,
        min_stats: int = 96,
        size: int = 1,
        require_price_agreement: bool = True,
    ) -> None:
        self.interval = interval
        self.lookback = lookback        # bars of flow in the imbalance
        self.horizon = horizon          # bars held after entry
        self.z_entry = z_entry          # standard deviations above the mean
        self.min_ratio = min_ratio      # absolute floor on the imbalance
        self.cooldown = cooldown        # bars to wait after an exit
        self.stats_len = stats_len      # observations in the adaptive window
        self.min_stats = min_stats      # observations before it is trusted
        self.size = size
        self.require_price_agreement = require_price_agreement

        # Rolling distribution of the imbalance, kept as running sums so the
        # per-bar cost is constant. Recomputing a statistic over the window on
        # every bar is the classic way to spend the wall-clock budget.
        self.ratios: deque[float] = deque(maxlen=stats_len)
        self._sum = 0.0
        self._sumsq = 0.0

        self.bar_count = 0
        self.entry_bar: int | None = None
        self.last_exit_bar = -10 ** 9

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    # ----------------------------------------------------------------- helpers

    def _levels(self) -> tuple[float, float]:
        """Entry thresholds for long and short, from what has been seen."""
        n = len(self.ratios)
        if n < self.min_stats:
            return self.min_ratio, -self.min_ratio
        mean = self._sum / n
        var = self._sumsq / n - mean * mean
        if var < 0.0:
            var = 0.0
        sd = var ** 0.5
        return (
            max(self.min_ratio, mean + self.z_entry * sd),
            min(-self.min_ratio, mean - self.z_entry * sd),
        )

    def _push(self, ratio: float) -> None:
        if len(self.ratios) == self.ratios.maxlen:
            old = self.ratios[0]
            self._sum -= old
            self._sumsq -= old * old
        self.ratios.append(ratio)
        self._sum += ratio
        self._sumsq += ratio * ratio

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        self.bar_count += 1

        window = ctx.history(self.interval).last(self.lookback)
        if len(window) < self.lookback:
            return                      # warmup; a legitimate state

        volume = 0
        delta = 0
        for bar in window:
            volume += bar.volume
            delta += bar.delta
        if volume <= 0:
            return
        ratio = delta / volume

        long_level, short_level = self._levels()
        self._push(ratio)               # accumulate during warmup as well

        if not ctx.actionable:
            return

        # --- manage an open position: the exit is a clock, not a level ------
        if ctx.position:
            if self.entry_bar is None:
                self.entry_bar = self.bar_count
            if ctx.working_order_ids:
                return                  # an exit is already in flight
            if self.bar_count - self.entry_bar >= self.horizon:
                ctx.close()
            return

        if ctx.working_order_ids:
            return
        if self.bar_count - self.last_exit_bar < self.cooldown:
            return

        first = window[0]
        last = window[-1]

        if ratio >= long_level:
            if self.require_price_agreement and not (last.close > first.open):
                return
            ctx.buy(size=self.size, tag="cvd-long")
            return

        if ratio <= short_level:
            if self.require_price_agreement and not (last.close < first.open):
                return
            ctx.sell(size=self.size, tag="cvd-short")

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self.entry_bar = None
            self.last_exit_bar = self.bar_count
        else:
            self.entry_bar = self.bar_count

    def on_lifecycle(self, ctx) -> None:
        # No order id is carried across callbacks: the position and the bar
        # clock are the only state, so an expiry cannot leave a stale id here.
        pass
