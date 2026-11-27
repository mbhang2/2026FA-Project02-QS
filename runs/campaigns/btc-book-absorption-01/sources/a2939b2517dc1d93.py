"""Absorption fade on the OKX BTC-USDT perpetual -- eighteenth attempt: the
measured maximum restored, and the held test moved to the level that actually
absorbs.

The claim and the direction are the campaign's and they do not change. After a
directional move, aggressive flow that runs into displayed liquidity which holds
marks a turn. **A short entry follows absorbed buying after a rise; a long entry
follows absorbed selling after a fall.** No path in this file enters with the
flow.

Where the campaign stands
-------------------------
    #0  -0.2927   6,341   market entry, tight bracket
    #1  -0.1051   6,587   market entry
    #2  -0.1044   3,054   mid-limit entry (adversely selected)
    #3  -0.1282   4,969   sigma bracket, market
    #4  -0.1522   2,237   sigma bracket, mid-limit
    #5  not scored: 156/960 sessions exposed, under the 70% floor
    #6  -0.0615   1,189   market, 1 round trip/session, 80/55
    #7  -0.0901   2,715   market, <=4/session
    #8  -0.0047   1,547   market, 1/session, wide bracket
    #9  +0.0427   1,213   maker at the touch, 80/55
    #10 +0.0479   1,231   maker inside the spread, stricter held test, 80/55
    #11 -0.1073   2,329   maker at touch, sigma move, 2 round trips
    #12 +0.0563   1,633   90/60, absolute stall cap, size 500
    #13 +0.0793   1,150   100/75, 4h clock
    #14 +0.0817   1,132   120/90, 5h clock
    #15 +0.1397   1,161   125/90, 6h clock          <-- best measured
    #16 +0.1044   1,144   145/105, 8h clock
    #17 +0.0942   1,135   150/112.5, 8h clock

What the last five rows establish
---------------------------------
The bracket-width lever that looked linear through #13 has stopped paying, and
the last two rows say so twice.

#16 -> #17 changed exactly one thing -- the width, 145/105 to 150/112.5, a 1.11x
product at an unchanged 8h clock -- and lost 10% of the score. There is no
clock story available for that pair: same hold, same ladder, same entry, same
signal, and a trade count within 1%. It is a clean measurement of the slope of
score against width in the wide regime, and the slope is **negative**: roughly
-3% of score per 1% of width, at 8h.

That matters because it means the a*b expansion of the two-barrier problem is
not the governing relation out here. a*b*mu/sigma^2 is a first-order expansion
in mu, and it says that widening both legs multiplies the captured drift; what
it cannot say is that mu is constant in width. The reversal drift this campaign
is fading is a finite displacement -- the aggressors who were absorbed do not
push price back 150bp, they push it back some fixed fraction of the move they
created -- so past the point where the target exceeds the available reversal the
extra width buys nothing and only lengthens the time the trade sits exposed to
whatever the day is doing. #15 sat at 125/90. #16 and #17 walked past it and
paid.

So the width is done. #15's 125/90 is the measured maximum in this family, and
the two rows above it are worse by 25% and 32%.

What this file changes
----------------------
Two things, and only one of them is a change of numbers.

**1. The geometry returns to #15 exactly**: target 125bp, stop 90bp, time stop
360 one-minute bars, size 100, one round trip per UTC day, ladder untouched.
Nothing here is a new guess about width or runway; it is the best point the
campaign has measured, restored.

**2. The held test is moved to the level that actually absorbs.** This is the one
part of the conjunction that has never been measured at the resolution the claim
implies, and it is the part the claim is *about*.

Everything since #10 tests the sum of the top **five** levels: at least 0.80x its
own 60-bar median, 0.80x the other side's top five, and 0.70x what it was at the
start of the stall. That is a real test of a real thing -- a heavy book on the
absorbing side -- but it is not a test of a *wall*. Five levels of a BTC
perpetual sum several price ticks of depth, and an aggressor sweeping into the
offer never touches levels two through five until level one is gone. A book can
pass every one of those three tests on the strength of levels three to five
while the touch itself is thin, and in that case there was nothing there to
absorb the flow: price simply walked up through an empty top of book, which is
the continuation the campaign says is the *other* outcome, entered as if it were
the signal.

So the touch is now tested directly, alongside the existing top-five test rather
than replacing it:

  * the absorbing side's **best** price level holds at least 0.55x its own
    60-bar median size, and
  * at least 0.45x the opposite side's best level.

Both are deliberately lenient. Top-of-book size on this instrument flickers on
the millisecond scale and the row `ctx.book()` hands us is a single snapshot;
requiring the touch to be merely *not thin* rather than *heavy* keeps the test a
floor rather than a coin flip. The relaxed coverage path scales both down with
the same 0.6/0.5 factors the top-five test already uses, so the insurance branch
stays the same conjunction, weaker -- never a different one.

This is a tightening, which is the direction every measured improvement in this
campaign has come from (#10's refill test, #9's maker entry, the coverage ladder
rather than a looser signal), and it removes trades rather than adding them,
which is the other thing every measured improvement has done.

What is deliberately not changed
--------------------------------
The signal of #10/#13/#15, verbatim, apart from the added touch test above: a
move of 25-150bp over the prior 20 bars, the last 5 bars stalled to within 40%
of it, aggressive flow over those 5 bars at 0.10 of traded volume and still
one-sided, the absorbing side's best price stationary to 5bp, and its top-five
size at least 0.80x its own 60-bar median, 0.80x the other side, and 70% of what
it was at the start of the stall.

Nor the entry, which is the largest measured effect in the campaign: #6 and #9
share a signal, a bracket and within 2% of a trade count and differ only in
whether the entry crosses or rests, and they differ by 0.104. The maker limit a
quarter of the spread inside the touch fills only when the aggressive flow the
claim names comes back to it, so it selects a different and better population,
and it sits ahead of the queue at a price where nothing is displayed rather than
behind the very wall that generated the signal. TIF DAY, cancelled and retried
after three bars, six attempts a session.

Nor the coverage ladder. Full conjunction while more than five hours remain, the
same conjunction with looser magnitudes down to two hours, then the same relaxed
signal as a market order -- the direction never changes on any path, so the
insurance branch is a weaker test of the claim and never a test of its negation.

Nor size (100 lots -- the score has never moved in a way size explains) nor the
one-round-trip-per-UTC-day rule (#11 doubled the round trips and lost 0.155).

Housekeeping, all inherited and all load-bearing: 1m bars with `emit_empty` so
the pre-close flatten always has a callback; orders only while `ctx.actionable`;
a round trip counted on the fill, not the submission, and never a reversal;
everything working cancelled and the position flattened five minutes before
`ctx.session_end`, so nothing is carried into a funding print; `None` from
buy/sell read as "no order exists"; no order id kept across callbacks, so a DAY
expiry cannot leave a stale reference; every rolling window on `self`, because
`ctx.book()` carries no history and `ctx.history()` is a bounded push window; and
a missing or stale book row never recorded as a collapse in displayed size.
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
        top0_hold: float = 0.55,
        top0_vs_other: float = 0.45,
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
        self.top0_hold = float(top0_hold)
        self.top0_vs_other = float(top0_vs_other)
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
        self._ask_top: deque = deque(maxlen=book_cap)
        self._bid_top: deque = deque(maxlen=book_cap)

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
            ask, bid, ask_depth, bid_depth, ask_top, bid_top = book
            # Only real rows enter the histories.  A missing or stale row is
            # not a collapse in displayed size, and recording it as one would
            # make every feed gap look like the book being eaten.
            self._ask_px.append(ask)
            self._bid_px.append(bid)
            self._ask_depth.append(ask_depth)
            self._bid_depth.append(bid_depth)
            self._ask_top.append(ask_top)
            self._bid_top.append(bid_top)
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
        """(ask, bid, top-5 ask size, top-5 bid size, level-0 ask, level-0 bid).

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
            ask_sizes = [float(x) for x in list(book.ask_sizes)[:5]]
            bid_sizes = [float(x) for x in list(book.bid_sizes)[:5]]
            if not ask_sizes or not bid_sizes:
                return None
            ask_top = float(ask_sizes[0])
            bid_top = float(bid_sizes[0])
            ask_depth = float(sum(ask_sizes))
            bid_depth = float(sum(bid_sizes))
            if ask_depth <= 0.0 or bid_depth <= 0.0:
                return None
            if ask_top <= 0.0 or bid_top <= 0.0:
                return None
            return ask, bid, ask_depth, bid_depth, ask_top, bid_top
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
            own0 = self._ask_top
            other0 = self._bid_top
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
            own0 = self._bid_top
            other0 = self._ask_top
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

        # --- the touch.  The top five levels can be heavy while the best price
        #     is thin, and an aggressor sweeping into an empty touch was never
        #     absorbed by anything -- price walked, which is continuation, not
        #     the signal.  Deliberately a floor and not a filter for size.
        if len(own0) < need or len(other0) < 1:
            return 0
        hist0 = [v for v in list(own0)[-self.depth_window:] if v > 0.0]
        if len(hist0) < need:
            return 0
        ref0 = median(hist0)
        now0 = float(own0[-1])
        other_top = float(other0[-1])
        if ref0 <= 0.0 or now0 <= 0.0 or other_top <= 0.0:
            return 0
        if now0 < self.top0_hold * hold_mult * ref0:
            return 0
        if now0 < self.top0_vs_other * other_mult * other_top:
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
