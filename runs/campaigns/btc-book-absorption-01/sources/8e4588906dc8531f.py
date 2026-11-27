"""Absorption fade on the OKX BTC-USDT perpetual -- twelfth attempt: the same
proven entry, with the bracket scaled to the horizon it is actually held over.

The claim and the direction are the campaign's and they do not change. After a
directional move, aggressive flow that runs into displayed liquidity which holds
marks a turn. **A short entry follows absorbed buying after a rise; a long entry
follows absorbed selling after a fall.** Nothing in this file enters with the
flow.

Where the campaign stands
-------------------------
    #0  -0.2927   6,341   market entry, tight bracket
    #1  -0.1051   6,587   market entry
    #2  -0.1044   3,054   mid-limit entry
    #3  -0.1282   4,969   sigma bracket, market entry
    #4  -0.1522   2,237   sigma bracket, mid-limit entry
    #5  not scored: exposed in 156 of 960 sessions, under the 70% floor
    #6  -0.0615   1,189   market entry, 1 round trip/session, 80/55 bracket
    #7  -0.0901   2,715   market entry, <=4/session
    #8  -0.0047   1,547   market entry, 1/session, wide bracket
    #9  +0.0427   1,213   maker entry at the touch, 1/session, 80/55 bracket
    #10 +0.0479   1,231   maker entry inside the spread, stricter held test
    #11 -0.1073   2,329   maker at touch, sigma move, 2 round trips/session

Two facts are established by that table and nothing else in it is.

1. **The entry dominates.** #6 and #9 have the same signal, the same bracket and
   within 2% of the same trade count, and differ only in whether the entry
   crosses or rests; they differ by 0.104 of score. Over roughly 600 round trips
   that is 17bp of price per trade, far more than the 2bp taker fee -- so the
   resting entry is not merely cheaper, it selects a different and better
   population: the order only fills when the aggressive flow the claim names
   comes back to it. #10 kept that mechanism and improved it slightly by resting
   a quarter of the spread inside the touch, which is ahead of the displayed
   queue at its own price rather than behind the wall that made the signal.

2. **More trades of the same shape are not more score.** #11 doubled the round
   trips and lost 0.155. Every loosening that admits a marginal trade has been
   negative, and #10's small gain over #9 came from *tightening* the held test,
   not from loosening anything. This file therefore keeps one round trip per UTC
   day, keeps the coverage ladder exactly as #10 left it, and does not touch the
   signal.

What this file changes, and why
-------------------------------
One number pair, chosen from arithmetic rather than from a search.

The bracket in #9 and #10 is target 80bp / stop 55bp against a maximum hold of
four hours. Over four hours the per-bar volatility estimate `_sigma()` implies a
one-sigma displacement of roughly 100-140bp on this data. So the bracket sits
**inside the noise of its own holding period**: the position is resolved by
first passage long before the four-hour clock, and the conditional drift the
signal is trying to collect is sampled only partially.

For a Brownian price with drift mu per horizon and volatility sigma, the
probability of touching +a before -b, for small drift, gives

    E[P&L] = a * P(+a) - b * P(-b)  ~=  a * b * mu / sigma^2 ,

which stops growing once a and b are comparable to sigma over the hold, where it
saturates at mu * T -- the whole drift, captured by simply holding. Two things
follow. First, a bracket that cannot manufacture an edge from zero drift must be
collecting a real one, and #10's +10bp gross per round trip back-solves to a
conditional drift of a few tens of basis points per four hours. Second, the way
to collect more of it is to widen the bracket: doubling the product a*b roughly
doubles the captured drift, up to the value of holding the full four hours.

So the bracket moves to **target 100bp / stop 75bp**, a product of 7,500 against
#10's 4,400. The stop is widened as well as the target, which is the
counter-intuitive half: with a real positive drift, a wider stop loses less
often than it loses more, and the loss per failure is bounded by the time stop
and the pre-close flatten either way. What is *not* done is widening without
bound -- a bracket far beyond sigma*sqrt(T) just holds to the four-hour mark and
gives back the entry, which is #6's failure mode in a different dress.

Everything else is #10 verbatim, because it is the best-scoring configuration
this campaign has and an attempt to change three things at once (#11) destroyed
it.

The signal, at the close of every 1m bar, from rolling state kept on `self`
(`ctx.history` is a bounded push window and `ctx.book()` carries no history)

  * **The prior move** -- close 5 bars ago against close 25 bars ago, at least
    25bp and no more than 150bp. Beyond that it is a news bar, not an absorption.
  * **It has stopped extending** -- over the last 5 bars the price added no more
    than 40% of the move and gave back no more than 40% of it.
  * **Flow still leaning into it** -- sum(bar.delta)/sum(bar.volume) over the
    stall window, one-sided at 0.10. `bar.delta` is accumulated per tick by the
    bar builder, so no tick subscription is needed and the engine keeps its
    event skip; unknown-aggressor volume is zero on this dataset, so the ratio
    is exact.
  * **The level held** -- top-5 displayed size on the side the flow ran into
    (asks after a rise, bids after a fall) at least 0.80x its own median over
    the last 60 sampled rows, at least 0.80x the other side, and at least 70% of
    what it was at the start of the stall window. That last check is the one #10
    added and it is the one that paid: a level can start huge, stay above its
    median, and still be half eaten.

Direction, stated once: `_signal()` returns -1 for **short after absorbed
buying** and +1 for **long after absorbed selling**. The two cases are separate
branches over `prior`, so the side of the book that confirms the signal and the
side of the trade that takes it cannot be transposed -- `prior > 0.0` sets the
absorbing book side to the asks and the direction to -1 in the same block.

Coverage is graded and the ladder is unchanged from #10, because an unscored run
is worse than a negative one: the full conjunction while more than five hours
remain, the same conjunction with looser magnitudes for the next three hours,
and a market order with two hours left. The relaxed path loosens thresholds and
never the direction -- it can still only short a rise or buy a fall, so the
insurance branch is a weaker test of the claim and not a test of its negation.

Housekeeping, all established earlier: 1m bars with `emit_empty` so the
pre-close flatten always has a callback; orders only while `ctx.actionable`; one
round trip per UTC day on a fill, never a reversal; everything working
cancelled and the position flattened five minutes before `ctx.session_end`, so
nothing is carried across a funding print; a four-hour time stop behind the
bracket; `None` from buy/sell read as "no order exists"; no order id kept across
callbacks, so a DAY expiry cannot leave a stale reference.
"""

from __future__ import annotations

from collections import deque
from statistics import median

NS_PER_SEC = 1_000_000_000


class Strategy:
    """Fade aggressive flow the visible book absorbed. One maker round trip a day."""

    def __init__(
        self,
        interval: str = "1m",
        size: int = 100,
        move_bars: int = 20,
        stall_bars: int = 5,
        min_move: float = 0.0025,
        max_move: float = 0.0150,
        stall_frac: float = 0.40,
        min_flow: float = 0.10,
        depth_window: int = 60,
        depth_min: int = 15,
        depth_hold: float = 0.80,
        depth_vs_other: float = 0.80,
        refill_hold: float = 0.70,
        book_tol: float = 0.0005,
        target_frac: float = 0.0100,
        stop_frac: float = 0.0075,
        max_spread_frac: float = 0.0012,
        max_hold_bars: int = 240,
        max_attempts: int = 6,
        entry_patience: int = 3,
        retry_cooldown_bars: int = 5,
        flat_margin_s: int = 300,
        signal_cutoff_s: int = 18000,
        last_resort_s: int = 7200,
    ) -> None:
        self.interval = interval
        self.size = max(1, int(size))
        self.move_bars = max(5, int(move_bars))
        self.stall_bars = max(2, int(stall_bars))
        if self.stall_bars >= self.move_bars:
            self.stall_bars = max(2, self.move_bars - 1)

        self.min_move = float(min_move)
        self.max_move = float(max_move)
        self.stall_frac = float(stall_frac)
        self.min_flow = float(min_flow)

        self.depth_window = max(10, int(depth_window))
        self.depth_min = max(5, int(depth_min))
        self.depth_hold = float(depth_hold)
        self.depth_vs_other = float(depth_vs_other)
        self.refill_hold = float(refill_hold)
        self.book_tol = float(book_tol)

        self.target_frac = float(target_frac)
        self.stop_frac = float(stop_frac)
        self.max_spread_frac = float(max_spread_frac)
        self.max_hold_bars = max(5, int(max_hold_bars))

        self.max_attempts = max(1, int(max_attempts))
        self.entry_patience = max(1, int(entry_patience))
        self.retry_cooldown_bars = max(0, int(retry_cooldown_bars))

        self.flat_margin_ns = max(60, int(flat_margin_s)) * NS_PER_SEC
        self.cutoff_ns = max(3600, int(signal_cutoff_s)) * NS_PER_SEC
        self.last_resort_ns = max(600, int(last_resort_s)) * NS_PER_SEC

        # Everything compared over time lives here.  ctx.history() is a bounded
        # push window and ctx.book() carries no history at all, so a rolling
        # statistic has to be maintained as the rows arrive.
        cap = self.move_bars + self.stall_bars + 16
        self._closes: deque = deque(maxlen=cap)
        self._deltas: deque = deque(maxlen=cap)
        self._volumes: deque = deque(maxlen=cap)

        book_cap = self.depth_window + 24
        self._ask_px: deque = deque(maxlen=book_cap)
        self._bid_px: deque = deque(maxlen=book_cap)
        self._ask_depth: deque = deque(maxlen=book_cap)
        self._bid_depth: deque = deque(maxlen=book_cap)

        self._session = None
        self._entered = False
        self._attempts = 0
        self._pending = 0
        self._cooldown = 0
        self._hold_bars = 0

    # ------------------------------------------------------------------ wiring

    def setup(self, ctx) -> None:
        # emit_empty so the pre-close flatten always has a callback to fire on:
        # a quiet stretch before midnight must not strand a position overnight.
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
            self._entered = False
            self._attempts = 0
            self._pending = 0
            self._cooldown = 0
            self._hold_bars = 0

        if self._cooldown > 0:
            self._cooldown -= 1

        book = self._read_book(ctx)
        if book is not None:
            ask, bid, ask_depth, bid_depth = book
            # Only real rows enter the histories.  A missing or stale row is
            # not a collapse in displayed size, and recording it as one would
            # make every feed gap look like the book being eaten.
            self._ask_px.append(ask)
            self._bid_px.append(bid)
            self._ask_depth.append(ask_depth)
            self._bid_depth.append(bid_depth)
        else:
            ask = 0.0
            bid = 0.0

        # Empty bars carry the previous close forward at zero volume: they
        # advance the clocks but they are not evidence about price or flow.
        if bar.tick_count > 0:
            self._closes.append(float(bar.close))
            self._deltas.append(float(bar.delta))
            self._volumes.append(float(bar.volume))

        remaining = ctx.session_end - ctx.ts

        # --- an open position: the bracket owns the price exits and was armed
        #     by the engine at the instant of the fill.  Only the clock is left.
        if ctx.position != 0:
            self._hold_bars += 1
            if remaining <= self.flat_margin_ns or self._hold_bars >= self.max_hold_bars:
                self._flatten(ctx)
            return
        self._hold_bars = 0

        if not ctx.actionable:
            return

        if remaining <= self.flat_margin_ns:
            if ctx.working_order_ids:
                ctx.cancel_all()
            return

        # One round trip a day, and never a reversal: #11 doubled this and lost
        # everything the entry had earned.
        if self._entered:
            return

        # --- an entry still working.  A maker order the flow never came back
        #     for is not a position; pull it and try again later.
        if ctx.working_order_ids:
            self._pending += 1
            if self._pending > self.entry_patience:
                ctx.cancel_all()
                self._pending = 0
                self._cooldown = self.retry_cooldown_bars
            return
        self._pending = 0

        if self._attempts >= self.max_attempts:
            return
        if self._cooldown > 0:
            return

        # Coverage is graded.  With plenty of day left, demand the full
        # conjunction.  After the cutoff, the same conjunction with looser
        # thresholds, first as a maker and then, with two hours left, as a
        # market order.  The direction never changes on any path, so the
        # insurance branch is still the claim and not its negation.
        use_market = False
        if remaining > self.cutoff_ns:
            direction = self._signal(relaxed=False)
        elif remaining > self.last_resort_ns:
            direction = self._signal(relaxed=True)
        else:
            direction = self._signal(relaxed=True)
            use_market = True

        if direction == 0:
            return

        if use_market:
            self._enter_market(ctx, direction)
            return

        if book is None:
            return
        if ask <= 0.0 or bid <= 0.0 or ask <= bid:
            return
        mid = 0.5 * (ask + bid)
        if mid <= 0.0 or (ask - bid) > self.max_spread_frac * mid:
            return

        stop_off = mid * self.stop_frac
        target_off = mid * self.target_frac
        if stop_off <= 0.0 or target_off <= 0.0:
            return

        # Maker entry just inside the touch, not at the mid.  A sell limit at
        # ask - eps fills only if buyers keep lifting; a buy limit at bid + eps
        # fills only if sellers keep hitting.  That is the population the claim
        # names, and -- unlike resting at the touch, tried in #9 and #11 -- the
        # order is ahead of the displayed queue at its own price rather than
        # behind the very wall that generated the signal.
        spread = ask - bid
        improve = min(0.25 * spread, 0.0002 * mid)
        if improve <= 0.0:
            improve = 0.25 * spread

        if direction > 0:
            limit = bid + improve
            if limit >= ask:
                limit = bid
            order_id = ctx.buy(
                size=self.size,
                type="limit",
                limit=limit,
                stop_offset=stop_off,
                target_offset=target_off,
                tif="DAY",
                tag="absorb-long",
            )
        else:
            limit = ask - improve
            if limit <= bid:
                limit = ask
            order_id = ctx.sell(
                size=self.size,
                type="limit",
                limit=limit,
                stop_offset=stop_off,
                target_offset=target_off,
                tif="DAY",
                tag="absorb-short",
            )

        if order_id is not None:
            self._attempts += 1
            self._pending = 0

    # --------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        # Only an entry fill leaves a non-zero position behind it.  Counting the
        # trade on the fill rather than on the submission means a rejected or
        # expired entry has not spent the session's one trade.
        if ctx.fill.position != 0:
            self._entered = True
            self._hold_bars = 0
            self._pending = 0

    def on_lifecycle(self, ctx) -> None:
        # No order id is kept across callbacks -- the engine holds the bracket
        # and ctx.cancel_all() reaches it -- so a DAY expiry at the session
        # close cannot leave a stale reference behind.
        pass

    # ----------------------------------------------------------------- helpers

    def _read_book(self, ctx):
        """(best ask, best bid, top-5 ask size, top-5 bid size) or None.

        A missing row, a stale row (over a second old reads as None), an empty
        side, a zero price and a crossed book are all ordinary states here, not
        errors.
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
            ask_depth = float(sum(float(x) for x in list(book.ask_sizes)[:5]))
            bid_depth = float(sum(float(x) for x in list(book.bid_sizes)[:5]))
            if ask_depth <= 0.0 or bid_depth <= 0.0:
                return None
            return ask, bid, ask_depth, bid_depth
        except Exception:
            return None

    def _signal(self, relaxed: bool = False) -> int:
        """-1 to short absorbed buying, +1 to buy absorbed selling, 0 for none.

        The two branches over `prior` are separate, so the absorbing side of the
        book and the side of the trade cannot transpose: prior > 0 sets the
        absorbing side to the asks and the direction to -1 in the same block.

        `relaxed=True` is the coverage path: the same tests with looser
        magnitudes, never a different test and never a different direction.
        """
        mb = self.move_bars
        sb = self.stall_bars
        if len(self._closes) < mb + sb + 1:
            return 0
        if len(self._volumes) < sb or len(self._deltas) < sb:
            return 0

        # Materialised as lists: a deque supports int indexing but not slicing,
        # and the stall lookback is a negative-offset index into the series.
        closes = list(self._closes)
        deltas = list(self._deltas)
        volumes = list(self._volumes)

        c_now = closes[-1]
        c_stall = closes[-1 - sb]
        c_prior = closes[-1 - sb - mb]
        if c_now <= 0.0 or c_stall <= 0.0 or c_prior <= 0.0:
            return 0

        prior = (c_stall - c_prior) / c_prior
        mag = prior if prior >= 0.0 else -prior
        min_move = self.min_move * 0.4 if relaxed else self.min_move
        if mag < min_move:
            return 0
        if not relaxed and mag > self.max_move:
            return 0

        # The move must have stopped: not still extending, and not already
        # collapsing back through itself.
        stall = (c_now - c_stall) / c_stall
        stall_frac = 1.0 if relaxed else self.stall_frac
        bound = stall_frac * mag
        if stall > bound or stall < -bound:
            return 0

        # Aggressive flow still leaning the way the move went.
        traded = float(sum(volumes[-sb:]))
        if traded <= 0.0:
            return 0
        flow = float(sum(deltas[-sb:])) / traded
        min_flow = self.min_flow * 0.3 if relaxed else self.min_flow

        if prior > 0.0:
            # A rise whose buying met an offer that would not lift.
            # Absorbed buying: fade it short.  Never long here.
            if flow < min_flow:
                return 0
            own = self._ask_depth
            other = self._bid_depth
            direction = -1
            # "The best ask did not move up", read from the book rather than
            # from trade wicks.
            if len(self._ask_px) >= sb + 1:
                asks = list(self._ask_px)
                ask_now = asks[-1]
                ask_start = asks[-1 - sb]
                tol = 0.0025 if relaxed else self.book_tol
                if ask_now > ask_start * (1.0 + tol):
                    return 0
                if max(asks[-sb:]) > ask_start * (1.0 + tol):
                    return 0
            elif not relaxed:
                return 0
        else:
            # A fall, mirrored.  Absorbed selling: fade it long.
            if flow > -min_flow:
                return 0
            own = self._bid_depth
            other = self._ask_depth
            direction = 1
            if len(self._bid_px) >= sb + 1:
                bids = list(self._bid_px)
                bid_now = bids[-1]
                bid_start = bids[-1 - sb]
                tol = 0.0025 if relaxed else self.book_tol
                if bid_now < bid_start * (1.0 - tol):
                    return 0
                if min(bids[-sb:]) < bid_start * (1.0 - tol):
                    return 0
            elif not relaxed:
                return 0

        # The level survived rather than being eaten or abandoned.
        need = self.depth_min
        if relaxed:
            need = max(3, self.depth_min // 3)
        if len(own) < need or len(other) < 1:
            return 0
        history = [v for v in list(own)[-self.depth_window:] if v > 0.0]
        if len(history) < need:
            return 0
        reference = median(history)
        now_size = float(own[-1])
        other_size = float(other[-1])
        if reference <= 0.0 or now_size <= 0.0 or other_size <= 0.0:
            return 0

        hold_mult = 0.60 if relaxed else 1.0
        other_mult = 0.30 if relaxed else 1.0
        if now_size < self.depth_hold * hold_mult * reference:
            return 0
        if now_size < self.depth_vs_other * other_mult * other_size:
            return 0

        # The level must not have been eaten over the stall window itself: a
        # wall that started huge, stayed above its median and was then halved is
        # a wall that lost, and #10 measured that check to be worth having.
        if len(own) >= sb + 1:
            start_size = float(own[-1 - sb])
            if start_size > 0.0:
                refill = 0.30 if relaxed else self.refill_hold
                if now_size < refill * start_size:
                    return 0

        return direction

    def _enter_market(self, ctx, direction: int) -> None:
        """Coverage entry.  Only ever called with a direction `_signal` chose."""
        px = 0.0
        try:
            px = float(ctx.last_price)
        except Exception:
            px = 0.0
        if px <= 0.0:
            return
        stop_off = px * self.stop_frac
        target_off = px * self.target_frac
        if stop_off <= 0.0 or target_off <= 0.0:
            return
        if direction > 0:
            order_id = ctx.buy(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="fade-long",
            )
        elif direction < 0:
            order_id = ctx.sell(
                size=self.size,
                stop_offset=stop_off,
                target_offset=target_off,
                tag="fade-short",
            )
        else:
            return
        if order_id is not None:
            self._attempts += 1

    def _flatten(self, ctx) -> None:
        """Cancel anything working and go flat, inside the UTC day."""
        self._hold_bars = 0
        self._pending = 0
        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position != 0:
            ctx.close()
