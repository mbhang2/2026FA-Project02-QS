"""Absorption fade on the OKX BTC-USDT perpetual -- seventeenth attempt: the
entry population of #15 is held exactly fixed and the *barrier is made
regime-neutral*.

The claim and the direction are the campaign's and they do not change. After a
directional move, aggressive flow that runs into displayed liquidity which holds
marks a turn. **A short entry follows absorbed buying after a rise; a long entry
follows absorbed selling after a fall.** Nothing in this file enters with the
flow.

Where the campaign stands
-------------------------
    #0  -0.2927   6,341   market entry, 25/30 bracket
    #1  -0.1051   6,587   market entry, 50/35
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
    #13 +0.0793   1,150   100/75, 4h time stop
    #14 +0.0817   1,132   120/90, 5h time stop
    #15 +0.1397   1,161   125/90, 6h time stop
    #16 +0.1044   1,144   145/105, 8h time stop
    #17 +0.0942   1,135   150/112.5, 8h time stop

What the last five rows actually say
------------------------------------
The first-order two-barrier expansion is

    E[P&L] ~= a * b * mu / sigma^2 ,

exact to first order in mu, with the ratio a/b setting the break-even hit rate
b/(a+b) rather than the edge -- and it only holds while both barriers are reached
before the clock. #13 -> #15 looked like a straight line in the product a*b;
#16 and #17 broke it in the same direction twice, and they broke it while
*holding the normalised geometry fixed*:

    trial   a      b      clock   sigma_clock   a/sigma   a*b      score
    #13    100     75     4h      125bp         0.80       7,500   +0.0793
    #14    120     90     5h      140bp         0.86      10,800   +0.0817
    #15    125     90     6h      153bp         0.82      11,250   +0.1397
    #16    145    105     8h      177bp         0.82      15,225   +0.1044
    #17    150   112.5    8h      177bp         0.85      16,875   +0.0942

Every one of those five sits at a/sigma between 0.80 and 0.86. The geometry was
already normalised, so the product is *not* what these rows are measuring -- and
the residual spread of 0.079 to 0.140 across five runs of essentially the same
normalised trade is the honest standard error of this measurement, roughly 0.03
per run. Chasing a sixth point along that line, which #16 and #17 already
regressed twice, is reading noise.

What is left, and why it is the barrier rather than the signal
--------------------------------------------------------------
The one thing the fixed-percentage bracket cannot do is hold a/sigma constant
*across sessions*. 125bp is 0.82 sigma in the median session, but this data's
six-hour realised volatility is not constant: in a quiet stretch a 125bp target
is 1.4 sigma away and the clock, not the barrier, ends the trade -- the exit the
campaign already diagnosed as the thing that gives the entry back (#14's
regression, cured in #15 only by buying more runway). In a wild stretch the same
125bp is 0.5 sigma, both barriers are hit inside the first hour, and the trade
degenerates into a coin flip on which one is touched first, worth
a*b*mu/sigma^2 ~ nothing against a 4bp round trip. Both tails are pure cost, and
both are pure cost for a reason that has nothing to do with the claim.

So this file scales the barrier with recent realised volatility, and it does so
*narrowly*: the target is clamped into [0.65, 1.00] x sigma_hold, where
sigma_hold is a robust estimate of the one-sigma displacement over the holding
period. The band is centred on 0.80 -- exactly where all five of the runs above
sit -- so a session whose sigma_6h falls between 1.25% and 1.92% gets #15's
bracket to the basis point, and the change only bites in the tails:

    sigma_6h   0.8%    1.25%   1.55%   1.92%   2.5%    3.5%
    target    1.00%   1.25%   1.25%   1.25%   1.63%   2.28%
    stop      0.72%   0.90%   0.90%   0.90%   1.17%   1.64%

The ratio a/b is held at 0.72 through every one of those rows, so the break-even
hit rate never moves; only the width in sigma moves, and only back toward the
0.80 the campaign has measured five times.

Why this cannot cost the coverage floor
---------------------------------------
The barrier does not touch the entry. The signal, the maker limit a quarter of
the spread inside the touch, the patience, the retry ladder, the attempt cap and
the one-round-trip-per-UTC-day rule are all verbatim from #15, so the *fill
population is identical by construction* -- the same orders at the same prices on
the same bars. Only the two exits that get armed when one of them fills differ.
Trade count and session exposure are therefore preserved exactly, which is the
property #5 lost and #11 paid 0.155 for.

What is deliberately not changed
--------------------------------
The signal, verbatim from #10, which has now paid three times: a move of 25-150bp
over the prior 20 bars, the last 5 bars stalled to within 40% of it, aggressive
flow over those 5 bars at 0.10 of traded volume and still one-sided, the
absorbing side's best price stationary to 5bp, and its top-5 size at least 0.80x
its own 60-bar median, 0.80x the other side and 70% of what it was at the start
of the stall. Every loosening of that conjunction has cost money and none is
attempted here.

Nor the entry -- the single largest measured effect in the campaign, 0.104
between #6 and #9 on the same signal and bracket. Nor the six-hour clock: it is
the one point of the width ladder that two independent directions agree on (5h
below it and 8h above it both scored worse), and it is now the horizon the
barrier is normalised *to*, so moving it would move two things at once.

Nor the coverage ladder (full conjunction above five hours remaining, the same
conjunction with looser magnitudes down to two, then a market order), because an
unscored run is worth less than a negative one and the direction never changes on
any path. Nor size (100 lots, well inside the displayed touch under the
book-queue-pessimistic model) nor the one-round-trip-per-UTC-day rule.

Housekeeping, all inherited and all load-bearing: 1m bars with `emit_empty` so
the pre-close flatten always has a callback; orders only while `ctx.actionable`;
a round trip counted on the fill, not the submission, and never a reversal;
everything working cancelled and the position flattened five minutes before
`ctx.session_end`, so nothing is carried across the 00:00 funding print; `None`
from buy/sell read as "no order exists"; no order id kept across callbacks, so a
DAY expiry cannot leave a stale reference; every rolling window on `self`,
because `ctx.book()` carries no history and `ctx.history()` is a bounded push
window; a missing or stale book row is never recorded as a collapse in displayed
size; and the volatility estimate is computed only when a signal has already
fired, so it costs nothing on the other 1,438 bars of the day.
"""

from __future__ import annotations

from collections import deque
from math import log, sqrt
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
        base_target_frac: float = 0.0125,
        base_stop_frac: float = 0.0090,
        vol_bars: int = 240,
        vol_floor: float = 0.0080,
        vol_cap: float = 0.0350,
        vol_ratio_hi: float = 1.00,
        vol_ratio_lo: float = 0.65,
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
        self.book_tol = float(book_tol)

        # #15's bracket, and the ratio it has to keep whatever the width does.
        self.base_target_frac = max(1e-5, float(base_target_frac))
        self.base_stop_frac = max(1e-5, float(base_stop_frac))
        self._stop_ratio = self.base_stop_frac / self.base_target_frac

        self.vol_bars = max(30, int(vol_bars))
        self.vol_floor = float(vol_floor)
        self.vol_cap = float(vol_cap)
        if self.vol_cap < self.vol_floor:
            self.vol_floor, self.vol_cap = self.vol_cap, self.vol_floor
        self.vol_ratio_hi = float(vol_ratio_hi)
        self.vol_ratio_lo = float(vol_ratio_lo)
        if self.vol_ratio_lo > self.vol_ratio_hi:
            self.vol_ratio_lo, self.vol_ratio_hi = (
                self.vol_ratio_hi,
                self.vol_ratio_lo,
            )

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

        # A separate, longer series for the volatility estimate.  Kept apart from
        # the signal window because it wants four hours and the signal wants
        # twenty-five minutes, and because it is read only when a signal fires.
        self._vol_closes: deque = deque(maxlen=self.vol_bars + 1)

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
            close = float(bar.close)
            self._closes.append(close)
            self._deltas.append(float(bar.delta))
            self._volumes.append(float(bar.volume))
            if close > 0.0:
                self._vol_closes.append(close)

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

        # Computed only once a signal has fired, so 1,400 bars a day never pay
        # for it.
        target_frac, stop_frac = self._barriers()

        if use_market:
            self._enter_market(ctx, direction, target_frac, stop_frac)
            return

        if book is None:
            return
        if ask <= 0.0 or bid <= 0.0 or ask <= bid:
            return
        mid = 0.5 * (ask + bid)
        if mid <= 0.0 or (ask - bid) > self.max_spread_frac * mid:
            return

        stop_off = mid * stop_frac
        target_off = mid * target_frac
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

    def _sigma_hold(self) -> float:
        """One-sigma displacement over the holding period, as a fraction.

        Robust rather than sample-sd: the closes contain the twenty-bar move the
        signal is built on, and a single outlier minute would otherwise set the
        width of the trade.  The median absolute log return times 1.4826 is the
        same estimator for a normal without that sensitivity.  Returns 0.0 when
        the estimate is unusable, and the caller then keeps #15's bracket.
        """
        try:
            closes = list(self._vol_closes)
            if len(closes) < 30:
                return 0.0
            rets = []
            prev = closes[0]
            for c in closes[1:]:
                if prev > 0.0 and c > 0.0:
                    rets.append(log(c / prev))
                prev = c
            if len(rets) < 20:
                return 0.0
            sigma_bar = 1.4826 * median([abs(r) for r in rets])
            if sigma_bar <= 0.0:
                return 0.0
            sigma = sigma_bar * sqrt(float(self.max_hold_bars))
            if sigma < self.vol_floor:
                sigma = self.vol_floor
            elif sigma > self.vol_cap:
                sigma = self.vol_cap
            return sigma
        except Exception:
            return 0.0

    def _barriers(self):
        """(target fraction, stop fraction) for this trade.

        #15's 125/90 unless recent volatility puts the target outside
        [lo, hi] x sigma_hold, in which case the whole bracket is scaled -- both
        legs by the same factor, so the ratio and therefore the break-even hit
        rate are untouched -- back to the nearer edge of the band.  The band is
        centred on 0.80 sigma, which is where all five of the runs from #13 to
        #17 sat, so a session of ordinary volatility gets the measured bracket
        to the basis point.
        """
        target = self.base_target_frac
        stop = self.base_stop_frac
        sigma = self._sigma_hold()
        if sigma > 0.0:
            hi = self.vol_ratio_hi * sigma
            lo = self.vol_ratio_lo * sigma
            if target > hi:
                target = hi
            elif target < lo:
                target = lo
            stop = target * self._stop_ratio
        if target <= 0.0 or stop <= 0.0:
            return self.base_target_frac, self.base_stop_frac
        return target, stop

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

    def _enter_market(self, ctx, direction: int, target_frac: float,
                      stop_frac: float) -> None:
        """Coverage entry.  Only ever called with a direction `_signal` chose."""
        px = 0.0
        try:
            px = float(ctx.last_price)
        except Exception:
            px = 0.0
        if px <= 0.0:
            return
        stop_off = px * stop_frac
        target_off = px * target_frac
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
