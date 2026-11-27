"""Cumulative volume delta continuation on the OKX BTC perpetual, traded rarely
and held for a horizon equal to the measurement window.

The claim under test
--------------------
Unusually large aggressive buying is followed by higher prices, and unusually
large aggressive selling by lower prices, over the next comparable window. The
sign of the signal is the sign of the position: this follows the flow, it does
not fade it.

What the two prior trials in this campaign established
------------------------------------------------------
Both lost, and the pattern across them is informative. Trial #0 ran 1,196 round
trips over 960 sessions and scored -0.1415; at 5bp per side that is roughly
0.125% of per-session return paid in fees alone, which accounts for essentially
the whole of its loss. Its gross edge was approximately zero -- not negative,
just absent. Trial #1 traded 185 times and scored -0.2349, which is far more loss
than its fees could possibly explain, so its per-trade gross was genuinely bad
(two-sided 2% brackets on a one-hour hold is a configuration where the stop is
touched by noise and the target is not).

So the binding constraint is not the signal, it is the fee schedule. 10bp a round
trip against an effect that, even in the best case, produces a few tens of basis
points over a few hours. A strategy that fires often cannot be saved by a better
filter; a strategy that fires rarely can at least be honest about what the flow
predicts, because its costs are negligible.

The design that follows from that
---------------------------------
* **Trade rarely.** Entry needs |ratio| at the 98th percentile of its own last
  30 days, the last bar's aggression must agree with the window's, and one
  position at a time with a cooldown after each exit. That lands on the order of
  a hundred and fifty round trips across the whole split rather than a thousand,
  which puts total fee drag in the region of 1% of notional over 960 sessions --
  a rounding error -- instead of 120%. Whatever the flow signal is worth, the
  measurement is no longer dominated by what it costs to look.

* **Measure flow as a normalised imbalance, not as contracts.** Over the last
  `lookback` hourly bars,

      ratio = sum(bar.delta) / sum(bar.volume)

  which lies in [-1, 1] and reads as "the share of the window's aggressive volume
  that was net buying, minus net selling". The campaign's question of how to call
  a flow "large" is answered twice: the ratio removes the level of BTC volume, so
  500 BTC of net buying means the same thing in a quiet 20k-BTC hour and a busy
  200k-BTC hour; and the cut-off is the rolling 98th percentile of |ratio| over
  the last 720 hours, so "large" tracks the regime rather than a constant chosen
  in 2023 and applied to 2026. A floor (`min_threshold`) stops a dead month from
  making a trivial imbalance count as extreme.

* **Horizon = measurement window = `hold` bars.** The claim is about "the next
  comparable window", so the exit is the clock. No target: a resting target is a
  different claim (where the move stops), and it would be filled by the
  continuation exactly when the continuation is strongest, which flatters the
  trade and mis-states the effect.

* **Market orders, deliberately.** The fill model is book-queue-pessimistic: a
  resting limit joins behind the displayed size and fills only when trades reach
  it. On a continuation entry that means the limits that fill are the ones where
  the expected move did not happen -- adverse selection baked into the entry --
  and the strategy never learns what the unfilled half would have paid. Paying the
  spread for a fill that actually occurs is the cheaper error, and 100ms of
  latency on one lot at hourly scale is not a meaningful slippage assumption.

* **Sums are running, not rescanned.** Delta, volume and closes live in bounded
  deques with running totals, so each bar is O(1). The only non-constant work is
  the percentile, which sorts 720 floats every 24 bars rather than every bar.

* **Risk is a catastrophe stop, not a thesis.** A close-of-bar check at 5% of
  adverse excursion from the average entry -- roughly two daily standard
  deviations for BTC -- held in code rather than armed as an engine order, so no
  bracket validation, DAY expiry or OCO event can end a run, and no order id is
  ever carried across a callback where it could go stale.

What is deliberately absent: any tick subscription (an order-flow feature at bar
resolution is free here, and ticks cost about 5x the wall clock for a quantity
`bar.delta` already gives exactly -- `unknown_volume` is zero on this feed), any
fade, any book input (not available for this instrument), any regime model, and
any second instrument.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """CVD continuation: follow rare, persistent, volume-normalised aggressor flow."""

    def __init__(self, interval="1h", lookback=6, hold=6, q=0.98,
                 min_threshold=0.05, rank_window=720, min_rank=240,
                 recompute_every=24, cooldown_bars=6, size=1, stop_bps=500.0):
        self.interval = interval
        self.lookback = lookback          # bars of aggressor flow in the signal
        self.hold = hold                  # bars held before the horizon exit
        self.q = q                        # quantile of |ratio| that counts as large
        self.min_threshold = min_threshold
        self.rank_window = rank_window    # hours of |ratio| history to rank
        self.min_rank = min_rank          # observations required before trading
        self.recompute_every = recompute_every
        self.cooldown_bars = cooldown_bars
        self.size = size
        self.stop_bps = stop_bps          # catastrophe stop, basis points

        # Rolling flow window with running sums: (delta, volume) per bar.
        self.flow = deque()
        self.sum_delta = 0.0
        self.sum_volume = 0.0
        self.closes = deque(maxlen=lookback + 1)

        # Rolling distribution of |ratio|.
        self._ranks = deque(maxlen=rank_window)
        self._threshold = None
        self._seen = 0

        self.held = 0
        self.cooldown = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx):
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # Features accumulate on every bar, warmup included: the bars are real,
        # only the orders would not be, and arriving at the first actionable bar
        # with no idea what "large" means would waste the warmup.
        self._push(bar)
        if len(self.flow) < self.lookback:
            return
        if self.sum_volume <= 0.0:
            return

        ratio = self.sum_delta / self.sum_volume
        self._ranks.append(abs(ratio))
        self._seen += 1
        if self._threshold is None or self._seen % self.recompute_every == 0:
            self._refresh_threshold()

        if not ctx.actionable:
            return

        if self.cooldown > 0:
            self.cooldown -= 1

        # --- in a position: the clock is the exit, the stop is insurance ------
        if ctx.position != 0:
            self.held += 1
            entry = ctx.average_entry_price
            price = ctx.last_price
            if entry > 0.0 and price > 0.0:
                if ctx.position > 0:
                    adverse = entry - price
                else:
                    adverse = price - entry
                if adverse > entry * self.stop_bps / 10000.0:
                    ctx.close()
                    return
            if self.held >= self.hold:
                ctx.close()
            return

        # --- flat -------------------------------------------------------------
        self.held = 0
        if ctx.working_order_ids or self.cooldown > 0:
            return

        threshold = self._threshold
        if threshold is None or abs(ratio) < threshold:
            return
        # Persistence: the most recent bar's aggression must still be on the
        # same side. A window made extreme by one old print is a different,
        # weaker statement than flow that has not let up.
        if self.flow[-1][0] * ratio <= 0.0:
            return

        if ratio > 0.0:
            ctx.buy(size=self.size, tag="cvd-long")
        else:
            ctx.sell(size=self.size, tag="cvd-short")

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx):
        if ctx.fill.position == 0:
            if self.held > 0:
                self.cooldown = self.cooldown_bars
            self.held = 0
        else:
            self.held = 0

    def on_lifecycle(self, ctx):
        pass

    # ---------------------------------------------------------------- helpers

    def _push(self, bar):
        if len(self.flow) >= self.lookback:
            old_delta, old_volume = self.flow.popleft()
            self.sum_delta -= old_delta
            self.sum_volume -= old_volume
        delta = float(bar.delta)
        volume = float(bar.volume)
        self.flow.append((delta, volume))
        self.sum_delta += delta
        self.sum_volume += volume
        self.closes.append(float(bar.close))

    def _refresh_threshold(self):
        if len(self._ranks) < self.min_rank:
            return
        ordered = sorted(self._ranks)
        idx = int(self.q * (len(ordered) - 1))
        self._threshold = max(float(ordered[idx]), self.min_threshold)
