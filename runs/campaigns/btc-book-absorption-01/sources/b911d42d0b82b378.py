"""Absorption fade on the OKX BTC-USDT perpetual -- nineteenth attempt: the same
round trip, the same barriers, and one number moved on the lever that actually
reaches the order book -- how deep inside the spread the maker entry rests.

The claim and the direction are the campaign's and they do not change. After a
directional move, aggressive flow that runs into displayed liquidity which holds
marks a turn. **A short entry follows absorbed buying after a rise; a long entry
follows absorbed selling after a fall.** Nothing in this file enters with the
flow.

Why the last file was not a trial
---------------------------------
#18 kept #15's bracket exactly -- 125/90 at a 360-bar clock -- and spent its one
change on trade discipline: `_entered`/`_open`/`_exited` recorded the exit fill
so that a reversal was made unexpressible rather than merely unlikely. The
synthetic pre-check came back byte-identical, and it was right to: the change
could not reach a single order. `_entered` is set by the entry fill and every
entry gate already returns on it, so `_exited` was a second copy of a flag that
was already closed. Nothing the engine could see moved, which means the run
would have been a re-measurement of #15 under a new name rather than a test of
anything.

So the bookkeeping stays -- it is correct, it costs nothing, and it states the
campaign's "each trade opens from flat and closes to flat" rule where the order
is placed -- but the trial has to be spent on something that changes the orders.

The lever, and why it is this one
---------------------------------
The single largest measured effect in this campaign is the entry: #6 and #9 share
a signal, a barrier geometry and a trade count within 2% and differ only in
whether the entry crosses or rests, and they differ by 0.104 of score. The
resting limit fills only when the aggressive flow the claim names comes back to
it, so it selects a different and better population. #10 then moved the resting
price from the touch to a quarter of the spread inside it and paid a further
0.005 (0.0427 -> 0.0479), which is the only tightening of the entry that has been
tried. Neither step has ever been undone, and no step beyond a quarter has been
taken.

Note how small the step was. On this instrument the spread is a fraction of a
basis point -- well under the 0.02%-of-mid ceiling on the offset, so the ceiling
has never bound and the offset has always been exactly a quarter of the spread --
and yet a quarter of a basis point of price was enough to move the score by
0.005. The fill population is acutely sensitive to where the order sits, which is
what makes this the right place to spend a trial and also what makes the step
size matter.

What this file changes
----------------------
The entry rests at **0.40 of the spread inside the touch** instead of 0.25, still
capped at 0.02% of the mid. One number.

It is the same lever, in the same direction, one step further -- not the mid. The
mid was tried twice, in #2 and #4, and both were strongly negative (-0.1044 and
-0.1522): an order at the mid is hit by the flow in both directions and stops
being a statement about which way the flow was going. 0.40 of the spread stays
inside the near touch, ahead of the displayed queue at its own price and behind
the wall that generated the signal, but asks the aggressive flow to come 0.4 of
the way back instead of 0.25 before it counts as the return the claim describes.

The expected effect has two halves and they pull opposite ways, which is why this
is worth measuring rather than assuming. The price is better by 0.15 of a spread
on every fill that happens -- free, and worth more than the 0.04% round-trip fee
on a per-trade basis is not the claim, but it is not nothing either. The number
of fills falls, because more of the flow that would have reached the old price
now turns before it. The campaign's one consistent finding is that the second
half has been the positive one every time it has been tested; this file measures
whether that is still true past a quarter of the spread.

What is deliberately not changed
--------------------------------
The bracket. #16 (145/105 at 480 bars) and #17 (150/112.5 at 480 bars) both
regressed #15's 0.1397, and the campaign's own reading of that ladder is that
#13 through #17 all sit at a/sigma between 0.80 and 0.86, their spread of 0.079
to 0.140 is the standard error of the measurement, and a sixth point along it
would be reading noise. So the geometry is #15's, the best measured
configuration: 125bp target, 90bp stop, 360 one-minute bars.

The signal, verbatim from #10, which has now paid three times: a move of 25-150bp
over the prior 20 bars, the last 5 bars stalled to within 40% of it, aggressive
flow over those 5 bars at 0.10 of traded volume and still one-sided, the absorbing
side's best price stationary to 5bp, and its top-5 size at least 0.80x its own
60-bar median, 0.80x the other side and 70% of what it was at the start of the
stall. Every loosening of that conjunction has cost money and none is attempted
here.

Nor the coverage ladder -- full conjunction while more than five hours remain,
the same conjunction with looser magnitudes down to two hours, then the same
relaxed signal as a market order -- because an unscored run is worth less than a
negative one and the direction never changes on any path. Nor size: 100 lots
keeps the order well inside the displayed touch under the book-queue-pessimistic
fill model, and the score has never moved in a way size explains.

Housekeeping, all inherited and all load-bearing: 1m bars with `emit_empty` so the
pre-close flatten always has a callback; orders only while `ctx.actionable`; a
round trip counted on the fill, not the submission, and closed by `_exited` the
moment the position goes flat, so a reversal is unexpressible; everything working
cancelled and the position flattened five minutes before `ctx.session_end`, so
nothing is carried across the 00:00 funding print; `None` from buy/sell read as
"no order exists"; no order id kept across callbacks, so a DAY expiry cannot
leave a stale reference; every rolling window on `self`, because `ctx.book()`
carries no history and `ctx.history()` is a bounded push window; and a missing or
stale book row is never recorded as a collapse in displayed size.
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
        target_frac: float = 0.0125,
        stop_frac: float = 0.0090,
        max_spread_frac: float = 0.0012,
        max_hold_bars: int = 360,
        max_attempts: int = 6,
        entry_patience: int = 3,
        retry_cooldown_bars: int = 5,
        flat_margin_s: int = 300,
        signal_cutoff_s: int = 18000,
        last_resort_s: int = 7200,
        entry_inside_frac: float = 0.40,
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

        # How far inside the touch the maker entry rests, as a fraction of the
        # spread.  0.25 is #10 and #15; 0.40 is this file.  Strictly inside --
        # 1.0 would be the mid, which #2 and #4 measured as adverse -- and
        # bounded above so a bad book cannot park the order absurdly deep.
        self.entry_inside_frac = min(0.90, max(0.05, float(entry_inside_frac)))

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
        # --- the trade's own lifecycle, which is what makes a reversal
        #     unexpressible rather than merely unlikely.
        #     _entered  -> an entry filled this session
        #     _open     -> a position is currently held from that fill
        #     _exited   -> that position has gone flat again; the session's one
        #                  round trip is spent and no order may be submitted
        #     _trade_dir-> the direction of the trade just completed, kept so
        #                  the record says which way it was
        self._entered = False
        self._open = False
        self._exited = False
        self._trade_dir = 0
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
            self._open = False
            self._exited = False
            self._trade_dir = 0
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
            self._open = True
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

        # One round trip a day, and never a reversal.  `_entered` says an entry
        # has filled; `_exited` says the position it produced has already been
        # closed.  Together they mean there is no state in this session in
        # which a second position can be opened, so a long-to-short flip is not
        # something this file can do even if a signal fires on the very next
        # bar.  #11 doubled the round trips and lost 0.155 learning that.
        if self._entered or self._exited:
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

        # A second, independent guard on the same rule.  If this session has
        # already carried a trade in the opposite direction, the new one is a
        # reversal by construction and is refused outright.  It cannot fire --
        # `_entered` above already returns -- but the rule is stated where the
        # order is placed, not only where the state is kept.
        if self._trade_dir != 0 and direction == -self._trade_dir:
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

        # Maker entry inside the touch, ahead of the displayed queue at its own
        # price rather than behind the wall that generated the signal.  The
        # depth of that offset is the campaign's single largest lever: #6 vs #9
        # measured 0.104 for resting at all, and #10 measured a further 0.005
        # for resting a quarter of the spread in rather than at the touch.
        # This file takes the next step on the same lever, to 0.40, still well
        # short of the mid that #2 and #4 measured as adverse.
        spread = ask - bid
        improve = min(self.entry_inside_frac * spread, 0.0002 * mid)
        if improve <= 0.0:
            improve = self.entry_inside_frac * spread

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
            self._trade_dir = 1 if direction > 0 else -1

    # --------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        # The trade has two halves and each is recorded from the fill that
        # actually produced it, not from the submission: a rejected or expired
        # entry has not spent the session's one trade, and a position that has
        # gone flat has spent it and cannot be reopened today.
        position = ctx.fill.position
        if position != 0:
            self._entered = True
            self._open = True
            self._hold_bars = 0
            self._pending = 0
        elif self._open:
            # The position that was open is now flat: the round trip is over,
            # and `_exited` closes every entry gate for the rest of the session.
            self._open = False
            self._exited = True
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
            self._trade_dir = 1 if direction > 0 else -1

    def _flatten(self, ctx) -> None:
        """Cancel anything working and go flat, inside the UTC day.

        The decision to close is the end of the trade, so `_exited` is set here
        as well as in `on_fill`: the clock exit and the pre-close exit are
        covered by the same statement as the bracket, and the position cannot
        be re-opened in the same direction or the opposite one afterwards.
        """
        self._hold_bars = 0
        self._pending = 0
        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position != 0:
            self._open = False
            self._exited = True
            ctx.close()
