"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling is followed by higher prices
over the next comparable window, and aggressive selling by lower prices. The
sign of the flow is the sign of the position. This file follows the flow; it
does not fade it.

What the four prior trials establish
------------------------------------
#0 (1h bars, 6-bar flow window, 6-bar hold, 0.85 rolling quantile) made 1,196
round trips and scored -0.1415. At 10 bp per round trip that is ~0.125% of
notional per session in fees — essentially the whole of the loss, which means
its *gross* edge was about zero. #1 (5m bars, ~1h hold) traded 185 times and
scored -0.2349, more loss than fees can explain, so its gross edge was genuinely
negative at the short horizon (#2's and #3's designs were both sound attempts to
cut the round-trip count and both were refused: 251 and 392 sessions of exposure
against a registered floor of 70%).

Those four results say two things at once, and this file is built around both:

1. **Cost is the binding constraint.** The flow signal has shown no gross edge
   large enough to survive 10 bp per round trip at horizons under a day. What is
   left to optimise is the fee bill.
2. **Exposure is a hard requirement.** Amendment 6 requires the strategy to be
   holding something in at least 70% of scored sessions — the point of the
   campaign is whether the effect is present across ordinary sessions, and a
   strategy that trades four times a month answers a different question. So
   "trade rarely" is not available: we must be in the market most of the time,
   which makes *turnover per unit of exposure* the only free variable.

The design that follows
-----------------------
Be in the market continuously, in the direction the cumulative aggressive flow
points, and pay for a round trip only when the flow has actually reversed.

* **Signal.** Over the last `lookback` hourly bars,

      ratio = sum(bar.delta) / sum(bar.volume)

  where `bar.delta` is buy_volume - sell_volume. Summing delta over the window
  *is* cumulative volume delta; dividing by the window's own total volume makes
  it dimensionless and removes the level of BTC volume, which is the campaign's
  question of how to call a flow "large" answered once. 500 BTC of net buying
  means the same thing in a quiet Asian morning and a US open. `unknown_volume`
  is zero on this feed — every trade carries its aggressor — so `delta` is the
  exact signed flow and the unknown-aggressor branch has nothing to decide.

* **Direction, not an episode.** When flat, enter on the sign of `ratio` with no
  threshold at all: exposure is the point, and there is nothing to whipsaw
  against when the alternative is being out of the market. Once in, hold until
  the flow reverses *strongly enough to be worth 10 bp* — which is what the band
  below decides.

* **The band is adaptive.** A flip requires the opposite-direction flow to
  exceed the rolling median of |ratio| over the last 30 days, floored at 0.25%.
  A fixed cut-off on a 24/7 asset is a bet on a volatility regime; a rolling
  quantile keeps the flip rule meaning the same thing in 2023 and 2026, which is
  exactly the "not one hour of the day / present across ordinary sessions"
  property the campaign asks a result to have. It is recomputed every 24 bars
  rather than every bar — sorting a few hundred floats on each of ~23,000 bars
  is needless work against a wall-clock budget.

* **A minimum holding period.** No flip within `min_hold` bars of the last one.
  This is the cost governor, stated plainly: with a 48-bar floor the flip count
  cannot exceed ~480 over a 960-session split and in practice lands well below
  it, which bounds total fees at a few percent of notional across the whole run
  instead of the ~120% trial #0 paid. It is the direct lesson of #0: it is not
  the signal that kills this strategy, it is the frequency with which it is
  acted on.

* **Flips are one order, not two.** Going from +1 to -1 sends a single sell of
  two lots rather than a close followed by a reopen, so there is no bar spent
  flat in between and no window in which the exit has filled and the entry has
  not. Nothing is held across callbacks, so nothing can go stale.

* **Orders are market.** The fill model is book-queue-pessimistic: a resting
  limit joins behind the displayed size and fills only when trades reach it,
  which on a continuation entry selects precisely the cases where the move never
  came. Paying the touch for a fill that actually happens is the cheaper error,
  and 100 ms of latency on a one-lot order is not meaningful slippage at hourly
  scale.

* **No stop.** A catastrophe stop on an always-in continuation strategy is a
  competing claim — it flattens on exactly the adverse excursion the flow said
  would not happen, then re-enters in the same direction and pays again. With a
  one-lot position the risk is a rounding error in the trial's P&L and the stop
  would be a pure fee. Risk here is managed by horizon and by the flip rule, not
  by a bracket, which also means no bracket-side validation, DAY expiry or OCO
  event can end the run.

Sums are running, not rescanned: `delta` and `volume` live in one bounded deque
with running totals, so each bar costs O(1). The only non-linear work is the
percentile, and it runs every 24 bars.

What is deliberately absent: a tick subscription (an order-flow feature at bar
resolution is free here and `bar.delta` carries it exactly; ticks cost ~5x the
wall clock for the same number), any fade (the campaign names the direction), any
book-based input (not available on this adapter), a regime model, a target or
bracket (a different claim — where the move stops), and any second instrument.

"""

from __future__ import annotations

from collections import deque


class Strategy:
    """CVD continuation: hold the sign of cumulative aggressive flow, flip rarely."""

    def __init__(self, interval="1h", lookback=72, min_hold=48,
                 rank_window=720, min_rank=96, band_quantile=0.5,
                 min_band=0.0025, recompute_every=24, size=1):
        self.interval = interval            # bar interval
        self.lookback = lookback            # bars of aggressor flow in the signal
        self.min_hold = min_hold            # bars between flips, at minimum
        self.rank_window = rank_window      # observations in the band's ranking
        self.min_rank = min_rank            # observations needed before a band exists
        self.band_quantile = band_quantile  # quantile of |ratio| used as the band
        self.min_band = min_band            # floor on the band
        self.recompute_every = recompute_every
        self.size = size

        # Rolling flow window with running sums: (delta, volume) per bar.
        self.flow = deque()
        self.sum_delta = 0.0
        self.sum_volume = 0.0

        # Rolling distribution of |ratio| for the adaptive flip band.
        self._ranks = deque(maxlen=rank_window)
        self._band = None
        self._seen = 0
        self._held = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx):
        # One subscription, but ask rather than assume: a bar from another
        # interval closing at this instant must not be mistaken for ours.
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # --- feature, updated on every bar, warmup included ------------------
        if len(self.flow) >= self.lookback:
            old_delta, old_volume = self.flow.popleft()
            self.sum_delta -= old_delta
            self.sum_volume -= old_volume
        delta = float(bar.delta)
        volume = float(bar.volume)
        self.flow.append((delta, volume))
        self.sum_delta += delta
        self.sum_volume += volume

        if len(self.flow) < self.lookback or self.sum_volume <= 0.0:
            return
        ratio = self.sum_delta / self.sum_volume

        self._ranks.append(abs(ratio))
        self._seen += 1
        if self._band is None or self._seen % self.recompute_every == 0:
            self._refresh_band()

        # Warmup bars are real, the orders would not be. The features above are
        # accumulated through them on purpose; only the trading is skipped.
        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return

        self._held += 1
        pos = ctx.position
        band = self._band

        # --- long: hold until the opposite flow is worth a round trip -------
        if pos > 0:
            if (band is not None and ratio < -band
                    and self._held >= self.min_hold):
                # One order takes +pos all the way to -1: no bar spent flat,
                # no half-applied flip if only the close had filled.
                ctx.sell(size=pos + 1, tag="cvd-flip-short")
                self._held = 0
            return

        # --- short: symmetric ------------------------------------------------
        if pos < 0:
            if (band is not None and ratio > band
                    and self._held >= self.min_hold):
                ctx.buy(size=(-pos) + 1, tag="cvd-flip-long")
                self._held = 0
            return

        # --- flat: enter on the sign, with no band ---------------------------
        # Being flat is the one state the campaign's exposure floor forbids, and
        # in a genuinely flat book there is no whipsaw to protect against.
        self._held = 0
        if ratio > 0.0:
            ctx.buy(size=self.size, tag="cvd-long")
        elif ratio < 0.0:
            ctx.sell(size=self.size, tag="cvd-short")

    # ---------------------------------------------------------------- helpers

    def _refresh_band(self):
        if len(self._ranks) < self.min_rank:
            return
        ordered = sorted(self._ranks)
        idx = int(self.band_quantile * (len(ordered) - 1))
        self._band = max(float(ordered[idx]), self.min_band)

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx):
        # Nothing to do: the flip is a single order, and the holding clock is
        # reset when the order is placed rather than when it fills. No order id
        # is kept across callbacks, so no expiry can make one stale.
        pass

    def on_lifecycle(self, ctx):
        pass
