"""CVD continuation on the OKX BTCUSDT perpetual.

The claim under test (campaign brief, verbatim): aggressive buying that
outweighs aggressive selling is followed by higher prices for a while, and
aggressive selling by lower prices. This file tests the *continuation* reading
of that claim and never the fade -- a signal is traded in the direction of the
imbalance, always.

How the claim is measured
-------------------------
* 30-minute time bars. Every bar already carries `buy_volume` / `sell_volume`,
  so `bar.delta` is signed aggressive flow at bar resolution and no tick
  subscription is needed. That matters: on this data a tick subscriber costs
  about 5x the wall clock of the same strategy on bars, and the order-flow
  content of `bar.delta` is identical.
* The signal is the **normalised** cumulative delta over the last L closed
  bars:

      imb = sum(bar.delta, last L) / sum(bar.volume, last L)

  Dividing by total traded volume is what makes "large" mean the same thing at
  03:00 and at 15:00, in 2023 and in 2026 -- the brief asks for exactly that.
  It is dimensionless and bounded in [-1, 1]: +1 means every contract in the
  window was bought by an aggressor, 0 means balanced.
* A position is taken only when |imb| clears a threshold, in the sign of the
  imbalance. A neutral window is not traded.

Horizon, entry, exit
--------------------
* Horizon: measured in bars and named explicitly. `lookback` = 16 bars = 8
  hours of flow; `max_hold` = 12 bars = 6 hours in the position. Both are held
  fixed inside this strategy, as the brief requires.
* Entry is a **market** order. The claimed edge is that price *keeps moving*;
  a resting limit on the far side would only fill when price came back to it,
  which is a different (mean-reverting) bet, and the fill model here only
  fills a resting order when trades go strictly through it. Paying the spread
  is the honest cost of taking a continuation.
* Exit is a time stop, plus a deliberately wide bracket (3% / 6% of price)
  armed by the engine at the instant of the fill. The bracket is not the
  thesis -- it is insurance against the tail move that would otherwise turn
  one bad bar into a career event, and it is wide enough that it almost never
  pre-empts the 6-hour time exit. The bracket legs are GTC so they do not die
  at the UTC-day boundary and leave the position naked, which is the one way
  a continuously-traded instrument can still be caught out by a calendar.

Costs
-----
5 bp per side, 10 bp round trip before depth. That is the binding constraint:
a signal that fires every bar cannot pay it. Hence a high threshold, an 8-hour
measurement window, a 6-hour hold and a cooldown of 6 bars after any exit --
turnover is deliberately throttled to roughly one position per several days.
Order-flow momentum that pays before fees and not after them is not a result
here, and this file is built so that if it does not pay, it does not churn.

Timing rules honoured
---------------------
Orders are queued on a bar close and become eligible on the next event, never
against the trade that closed the bar that produced the signal (T+1). The
protective bracket is armed by the engine at the fill, so the position is
never naked between the fill and the next callback. `ctx.history` is not read
at all: the rolling window is maintained on `self` from `ctx.bars`, which is
both cheaper and immune to `WindowTooShort`.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Trade in the direction of normalised cumulative delta."""

    def __init__(
        self,
        interval: str = "30m",
        lookback: int = 16,
        threshold: float = 0.25,
        max_hold: int = 12,
        cooldown: int = 6,
        stop_frac: float = 0.03,
        target_frac: float = 0.06,
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.lookback = int(lookback)
        self.threshold = float(threshold)
        self.max_hold = int(max_hold)
        self.cooldown = int(cooldown)
        self.stop_frac = float(stop_frac)
        self.target_frac = float(target_frac)
        self.size = int(size)

        # Rolling window of signed flow and total volume, one entry per bar.
        # Kept on self so the read is O(lookback) and never touches the
        # engine's bounded history window.
        self.deltas: deque = deque(maxlen=self.lookback)
        self.vols: deque = deque(maxlen=self.lookback)

        self.hold_bars = 0
        self.cooldown_left = 0
        self.was_in_position = False

    # ------------------------------------------------------------------ setup

    def setup(self, ctx) -> None:
        # One subscription: 30-minute bars, anchored to the UTC-day session
        # open. Everything the signal needs arrives on the bar.
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        # Accumulate on every bar, warmup included -- the window has to be
        # full before the first actionable bar can mean anything.
        self.deltas.append(float(bar.delta))
        self.vols.append(float(bar.volume))

        if not ctx.actionable:
            return

        # ---- in a position: count the hold, exit on time -------------------
        if ctx.position != 0:
            self.was_in_position = True
            self.hold_bars += 1
            if self.hold_bars == self.max_hold:
                # Pull the bracket first, then flatten. Both are DAY-agnostic
                # because the entry was GTC; nothing is left resting.
                ctx.cancel_all()
                ctx.close()
            return

        # ---- flat: a position just finished, start the cooldown -------------
        if self.was_in_position:
            self.was_in_position = False
            self.hold_bars = 0
            self.cooldown_left = self.cooldown

        if self.cooldown_left > 0:
            self.cooldown_left -= 1
            return

        # One position at a time. `working_order_ids` covers the engine-armed
        # bracket legs as well, so there is no path to a second entry.
        if ctx.working_order_ids:
            return
        if len(self.deltas) < self.lookback:
            return

        total = sum(self.vols)
        if total <= 0.0:
            return

        imb = sum(self.deltas) / total
        if imb > self.threshold:
            direction = 1
        elif imb < -self.threshold:
            direction = -1
        else:
            return

        price = float(ctx.last_price)
        if price <= 0.0:
            return

        # Offsets, not absolute levels: they are measured from the actual fill
        # and so cannot land on the wrong side of it after slippage.
        stop_off = price * self.stop_frac
        target_off = price * self.target_frac

        if direction > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tif="GTC",
                tag="cvd-long",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tif="GTC",
                tag="cvd-short",
            )

        if order_id is None:
            return          # action cap dropped it; no order exists
        self.hold_bars = 0

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx) -> None:
        # The bracket is armed by the engine at the instant of the entry fill,
        # so there is nothing to do here: the position is never unprotected.
        pass

    # ------------------------------------------------------------- lifecycles

    def on_lifecycle(self, ctx) -> None:
        # No order ids are kept across callbacks -- every order is placed and
        # then referred to only through ctx.position / ctx.working_order_ids,
        # so an expiry can never leave this strategy holding a stale id.
        pass
