"""Absorption fade on the OKX BTC-USDT perpetual: pay the cost, but only once
per unit of range traded for.

The claim under test is the campaign's, unchanged: after a directional move,
aggressive flow that runs into displayed liquidity which *holds* marks a turn
more often than a continuation. The trade is always the fade -- short absorbed
buying after a rise, long absorbed selling after a fall -- never with the flow.

What the six prior trials say, read as arithmetic rather than as six bets
-------------------------------------------------------------------------
Six attempts, four of them scored, none positive:

    #0  -0.2927   6,341 trades   target 25bp / stop 30bp   market entry
    #1  -0.1051   6,587 trades   target 50bp / stop 35bp   market entry
    #2  -0.1044   3,054 trades   target 40bp / stop 30bp   mid-limit entry
    #3  -0.1282   4,969 trades   target 1.5x / stop 1.0x   market entry
    #4  -0.1522   2,237 trades   7 sigma / 4.5 sigma       mid-limit entry
    #5  not scored: exposed on 156 of 960 sessions, under the 70% floor.

Backing the hit rate out of #1, which is the cleanest of them (market entry, so
the fill sample is unbiased, 50bp/35bp):

    per trade  pc - (1-p)s - cost = -1.6bp  ->  p is about 0.44
    random-walk baseline for 50/35          ->  p = 35/85 = 0.412

So the *gross* price edge of the fade at that bracket was roughly +2.4bp per
trade -- small, real, and the right sign. The round trip by market order costs
~4bp. The signal paid; the execution ate it and a little more.

Now compare #2, the maker-entry version. If a mid-limit entry really were free,
the same gross edge would have made #2 positive. It did not: #2's per-trade
result is about -3.4bp, which back-solves to a hit rate of 0.396 -- *below* the
random-walk baseline. A resting bid at the mid only fills when the price has
come down to it, so the fill price is systematically above the market that
exists at the fill instant. That adverse move is worth about the 2bp the maker
fee saved, and it also corrupts the sample: the trades you get are the ones that
were already going against you.

Two facts follow, and they drive every choice in this file:

  * **The entry costs ~2bp whichever way it is done.** Resting pays it as an
    adverse move, taking pays it as a fee. Six attempts is enough evidence for
    that; there is no cheap entry left to find.
  * **The exit is where cost can still be avoided.** The bracket's target leg is
    a resting limit (0bp on this run). Only the stop, the time stop and the
    pre-close flatten pay taker. So the cost of a trade is
    `2bp + P(not a target) * 2bp`, not `4bp`.

And the lever that actually moves the answer: the expected *gross* value of a
bracket is the probability edge times the width, `delta * (S + T)`, while the
cost is a constant few basis points. The break-even edge a signal needs is
therefore `cost / (S + T)` -- **it shrinks as the bracket widens**, and #0
(target 25 / stop 30) losing three times what #1 (target 50 / stop 35) lost at
the same trade count is exactly that effect showing up as data.

What this version changes
-------------------------
  1. **Width, not cleverness.** Target 100bp over a 65bp stop: 165bp of range,
     a 39.4% random-walk hit rate, and a break-even of 41.3% once the ~3.2bp
     expected cost is added. #1 needed 45.9% break-even on 85bp of range. On
     1m BTC bars this bracket resolves in roughly an hour, so four entries a
     session still fit inside a UTC day with room for the time stop.
  2. **Market entry, chosen deliberately.** #2 demonstrated that the maker
     discount is paid back as adverse selection. An unbiased sample of the
     signal is worth more than 2bp when the entire question is whether the
     signal has an edge.
  3. **Effort without result, measured as the claim states it.** Over one
     window, not a move-then-stall conjunction: over the last 20 bars the price
     travelled at least 35bp, and over the last 4 bars aggressive flow is still
     leaning that way (|sum(delta)/sum(volume)| >= 0.15, from `bar.delta`, which
     the bar builder accumulates per tick so no tick subscription is needed)
     while the price has stopped extending -- no more than 30% of the prior move
     added, no more than 60% given back, and no new extreme beyond the prior
     window's extreme by more than 8bp. That last test is the literal "the best
     ask did not move up"; a stall measured on closes alone is satisfied by a
     bar that traded through the offer and came back, which is the opposite of
     absorption. #5's version of this was right in spirit and far too
     restrictive in degree (60bp moves, an 8-bar stall, a 1.05x depth ratio and
     a 20-bar spacing killed it down to 156 sessions).
  4. **The book confirms, loosely.** The absorbing side's top-5 displayed size
     must be at least 0.90x its own median over the last 40 sampled rows -- it
     survived the flow rather than being eaten -- and at least 0.70x the other
     side's, so supply is comparable to demand rather than a thin book being
     swept. Both are ratios inside one series, so a change of lot convention
     cannot rescale them. Missing or stale rows are skipped, never recorded as
     zero: a feed gap must not read as a collapse in depth.

Exposure, because #5 proved it is a hard constraint
---------------------------------------------------
The floor is exposure on 70% of scored sessions, and the way to miss it is a
conjunction of rare conditions. So the trigger is a single window test that
fires several times on an ordinary day: a 35bp move over 20 minutes is under one
sigma on BTC, and the rest of the conjunction is measured over the same 4 bars.
Four entries a session, fifteen bars apart, eight attempts, ~3,000-4,000 round
trips over 960 sessions -- comparable to #2's count, on most days, without #0's
churn.

Housekeeping the earlier files already established: 1m bars with `emit_empty`
so the pre-close flatten always has a callback; everything working cancelled and
the position flattened five minutes before `ctx.session_end`, so every trade
opens from flat and closes back to flat inside its own UTC day and no position
is ever carried across a funding print; `ctx.actionable` checked before every
order; `None` from buy/sell read as "no order exists"; every rolling window on
`self`, because `ctx.book()` has no history at all and `ctx.history()` is a
bounded push window; no order id kept across callbacks.
"""

from __future__ import annotations

from collections import deque
from statistics import median

NS_PER_SEC = 1_000_000_000


class Strategy:
    """Fade aggressive flow that fails to move price against the visible book."""

    def __init__(
        self,
        interval: str = "1m",
        size: int = 100,
        move_bars: int = 20,
        stall_bars: int = 4,
        min_move: float = 0.0035,
        stall_cap: float = 0.30,
        giveback_cap: float = 0.60,
        new_extreme_tol: float = 0.0008,
        min_flow: float = 0.15,
        depth_hold: float = 0.90,
        depth_vs_other: float = 0.70,
        depth_window: int = 40,
        depth_min: int = 12,
        target_frac: float = 0.0100,
        stop_frac: float = 0.0065,
        max_spread_frac: float = 0.0010,
        max_hold_bars: int = 120,
        max_entries: int = 4,
        max_attempts: int = 8,
        min_spacing_bars: int = 15,
        flat_margin_s: int = 300,
        min_time_left_s: int = 7200,
    ) -> None:
        self.interval = interval
        self.size = max(1, int(size))
        self.move_bars = max(3, int(move_bars))
        self.stall_bars = max(2, int(stall_bars))
        self.min_move = float(min_move)
        self.stall_cap = float(stall_cap)
        self.giveback_cap = float(giveback_cap)
        self.new_extreme_tol = float(new_extreme_tol)
        self.min_flow = float(min_flow)
        self.depth_hold = float(depth_hold)
        self.depth_vs_other = float(depth_vs_other)
        self.depth_window = max(8, int(depth_window))
        self.depth_min = max(4, int(depth_min))
        self.target_frac = float(target_frac)
        self.stop_frac = float(stop_frac)
        self.max_spread_frac = float(max_spread_frac)
        self.max_hold_bars = max(4, int(max_hold_bars))
        self.max_entries = max(1, int(max_entries))
        self.max_attempts = max(1, int(max_attempts))
        self.min_spacing_bars = max(0, int(min_spacing_bars))
        self.flat_margin_ns = max(1, int(flat_margin_s)) * NS_PER_SEC
        self.min_time_left_ns = max(1, int(min_time_left_s)) * NS_PER_SEC

        # Everything compared over time lives here: ctx.history() is a bounded
        # push window and ctx.book() has no history at all.
        cap = self.move_bars + self.stall_bars + 12
        self._closes: deque[float] = deque(maxlen=cap)
        self._highs: deque[float] = deque(maxlen=cap)
        self._lows: deque[float] = deque(maxlen=cap)
        self._deltas: deque[float] = deque(maxlen=cap)
        self._volumes: deque[float] = deque(maxlen=cap)

        book_cap = self.depth_window + 24
        self._ask_depth: deque[float] = deque(maxlen=book_cap)
        self._bid_depth: deque[float] = deque(maxlen=book_cap)

        self._session: int | None = None
        self._entries = 0
        self._attempts = 0
        self._since_entry = 10 ** 9
        self._hold_bars = 0
        self._pending = 0

    # ------------------------------------------------------------------ wiring

    def setup(self, ctx) -> None:
        # emit_empty so the pre-close flatten always has a callback to fire on.
        ctx.subscribe(self.interval, emit_empty=True)
        # No callback of its own; this only makes ctx.book() readable.
        ctx.subscribe("book")

    # -------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._session = ctx.session
            self._entries = 0
            self._attempts = 0
            self._since_entry = 10 ** 9
            self._hold_bars = 0
            self._pending = 0

        if self._since_entry < 10 ** 9:
            self._since_entry += 1

        book = self._read_book(ctx)
        if book is not None:
            ask = book[0]
            bid = book[1]
            # Only real rows enter the depth history: a missing or stale row is
            # not a collapse in displayed size, and recording it as one would
            # make every feed gap look like the book being eaten.
            self._ask_depth.append(book[2])
            self._bid_depth.append(book[3])
        else:
            ask = 0.0
            bid = 0.0

        # Empty bars carry the previous close forward at zero volume: they
        # advance the clocks but they are not evidence about price or flow.
        if bar.tick_count > 0:
            self._closes.append(float(bar.close))
            self._highs.append(float(bar.high))
            self._lows.append(float(bar.low))
            self._deltas.append(float(bar.delta))
            self._volumes.append(float(bar.volume))

        remaining = ctx.session_end - ctx.ts

        # --- an open position: the bracket owns the price exits and was armed
        #     at the instant of the fill. Only the time stop and the day
        #     boundary are left to do here.
        if ctx.position != 0:
            self._pending = 0
            self._hold_bars += 1
            if remaining <= self.flat_margin_ns or self._hold_bars >= self.max_hold_bars:
                self._flatten(ctx)
            return
        self._hold_bars = 0

        if not ctx.actionable:
            return

        if remaining <= self.flat_margin_ns:
            self._pending = 0
            if ctx.working_order_ids:
                ctx.cancel_all()
            return

        # --- an entry that has not filled yet. A market order that has not
        #     filled after three bars is not going to; drop it.
        if ctx.working_order_ids:
            self._pending += 1
            if self._pending > 3:
                ctx.cancel_all()
                self._pending = 0
            return
        self._pending = 0

        if self._entries >= self.max_entries:
            return
        if self._attempts >= self.max_attempts:
            return
        if self._since_entry < self.min_spacing_bars:
            return
        if remaining < self.min_time_left_ns:
            return
        if book is None:
            return

        if ask <= 0.0 or bid <= 0.0 or ask <= bid:
            return
        midpoint = 0.5 * (ask + bid)
        if midpoint <= 0.0:
            return
        if (ask - bid) > self.max_spread_frac * midpoint:
            return
        last = float(ctx.last_price)
        if last <= 0.0 or abs(midpoint - last) > 0.01 * last:
            return

        direction = self._signal()
        if direction == 0:
            return

        stop_off = midpoint * self.stop_frac
        target_off = midpoint * self.target_frac
        if stop_off <= 0.0 or target_off <= 0.0:
            return

        # Market entry: unbiased in the direction of the signal. The target leg
        # of the bracket rests as a limit and pays nothing; only the stop, the
        # time stop and the pre-close flatten pay the taker rate.
        if direction > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="absorb-long",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="absorb-short",
            )

        # None means the action cap dropped it; then no order exists.
        if order_id is not None:
            self._attempts += 1
            self._pending = 0

    # --------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        # Only an entry fill leaves a non-zero position behind it.
        if ctx.fill.position != 0:
            self._entries += 1
            self._since_entry = 0
            self._hold_bars = 0
            self._pending = 0

    def on_lifecycle(self, ctx) -> None:
        # No order id is kept across callbacks, so a DAY expiry at the session
        # close cannot leave a stale reference behind.
        pass

    # ----------------------------------------------------------------- helpers

    def _read_book(self, ctx):
        """(best ask, best bid, top-5 ask size, top-5 bid size) or None.

        A missing row, a stale row (over a second old reads as None), an empty
        side and a zero price are all ordinary states, not errors.
        """
        try:
            book = ctx.book()
        except Exception:
            return None
        if book is None:
            return None
        try:
            ask_prices = book.ask_prices
            bid_prices = book.bid_prices
            if len(ask_prices) == 0 or len(bid_prices) == 0:
                return None
            ask = float(ask_prices[0])
            bid = float(bid_prices[0])
            if ask <= 0.0 or bid <= 0.0 or ask <= bid:
                return None
            ask_depth = float(sum(float(x) for x in book.ask_sizes[:5]))
            bid_depth = float(sum(float(x) for x in book.bid_sizes[:5]))
            if ask_depth <= 0.0 or bid_depth <= 0.0:
                return None
            return ask, bid, ask_depth, bid_depth
        except Exception:
            return None

    def _signal(self) -> int:
        """-1 short after absorbed buying, +1 long after absorbed selling, 0 none.

        Effort without result, over one window and at one time: aggressive flow
        strongly one-sided in the direction of a completed move, the price no
        longer extending, and displayed size on the side the flow ran into still
        there.
        """
        mb = self.move_bars
        sb = self.stall_bars
        if len(self._closes) < mb + sb + 1:
            return 0

        closes = list(self._closes)
        highs = list(self._highs)
        lows = list(self._lows)
        deltas = list(self._deltas)
        volumes = list(self._volumes)
        if len(highs) < mb + sb + 1 or len(volumes) < sb:
            return 0

        c_now = closes[-1]
        c_stall = closes[-1 - sb]
        c_prior = closes[-1 - sb - mb]
        if c_now <= 0.0 or c_stall <= 0.0 or c_prior <= 0.0:
            return 0

        prior = (c_stall - c_prior) / c_prior
        if abs(prior) < self.min_move:
            return 0

        # The move must have stopped: not still extending, and not already
        # collapsing back through itself (that is a different, later trade).
        stall_ret = (c_now - c_stall) / c_stall
        if prior > 0.0:
            if stall_ret > self.stall_cap * prior:
                return 0
            if stall_ret < -self.giveback_cap * prior:
                return 0
        else:
            if stall_ret < self.stall_cap * prior:
                return 0
            if stall_ret > -self.giveback_cap * prior:
                return 0

        # "The best ask did not move up": no new extreme inside the stall
        # window, with a small tolerance for a wick. A stall measured on closes
        # alone is satisfied by a bar that traded through the offer and came
        # back, which is the opposite of absorption.
        if prior > 0.0:
            prior_extreme = max(highs[-(mb + sb):-sb])
            stall_extreme = max(highs[-sb:])
            if stall_extreme > prior_extreme * (1.0 + self.new_extreme_tol):
                return 0
            absorbing = True
        else:
            prior_extreme = min(lows[-(mb + sb):-sb])
            stall_extreme = min(lows[-sb:])
            if stall_extreme < prior_extreme * (1.0 - self.new_extreme_tol):
                return 0
            absorbing = False

        # Aggressive flow still leaning the way the move went: buyers still
        # lifting into an offer that will not yield, or sellers still hitting a
        # bid that will not fall. Normalised by traded volume, not fitted.
        traded = float(sum(volumes[-sb:]))
        if traded <= 0.0:
            return 0
        flow = float(sum(deltas[-sb:])) / traded
        if absorbing:
            if flow < self.min_flow:
                return 0
        else:
            if flow > -self.min_flow:
                return 0

        # Displayed size on the side the flow is leaning on: relative to its own
        # recent distribution, and comparable to the other side.
        own = self._ask_depth if absorbing else self._bid_depth
        other = self._bid_depth if absorbing else self._ask_depth
        if len(own) < self.depth_min or len(other) < 1:
            return 0
        history = [v for v in list(own)[-self.depth_window:] if v > 0.0]
        if len(history) < self.depth_min:
            return 0
        reference = median(history)
        now_size = own[-1]
        other_size = other[-1]
        if reference <= 0.0 or now_size <= 0.0 or other_size <= 0.0:
            return 0
        # A level that survived the flow rather than being eaten.
        if now_size < self.depth_hold * reference:
            return 0
        # Supply comparable to demand, not a thin book being swept.
        if now_size < self.depth_vs_other * other_size:
            return 0

        return -1 if absorbing else 1

    def _flatten(self, ctx) -> None:
        """Cancel anything working and go flat, before the UTC day turns."""
        self._hold_bars = 0
        self._pending = 0
        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position != 0:
            ctx.close()
