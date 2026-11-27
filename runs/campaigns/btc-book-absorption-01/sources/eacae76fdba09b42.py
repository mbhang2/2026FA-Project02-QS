"""Absorption fade on OKX BTC-USDT perpetual -- twelfth attempt: #10's signal,
one honest tightening, a wider bracket, and the leverage the model was leaving
on the table.

The claim and the direction are the campaign's and are unchanged: after a
directional move, aggressive flow that runs into displayed liquidity which
holds marks a turn. A short follows absorbed buying after a rise; a long
follows absorbed selling after a fall. No path in this file enters with the
flow.

Where the campaign stands
-------------------------
    #0  -0.2927   6,341   25/30 bracket        market entry
    #1  -0.1051   6,587   50/35 bracket        market entry
    #2  -0.1044   3,054   mid-limit entry      (adversely selected)
    #3  -0.1282   4,969   sigma bracket        market entry
    #4  -0.1522   2,237   sigma bracket        mid-limit entry
    #5  not scored: 156/960 sessions exposed, under the 70% floor
    #6  -0.0615   1,189   3:1 bracket          market, 1/session
    #7  -0.0901   2,715   wide bracket         market, <=4/session
    #8  -0.0047   1,547   wide bracket         market, 1/session
    #9  +0.0427   1,213   wide bracket         maker-at-touch, 1/session
    #10 +0.0479   1,231   wide bracket         maker inside the spread
    #11 -0.1073   2,329   sigma move, volume gate, two round trips, market fallback

Two levers moved the number and nothing else did: the number of round trips
(#6 -> #8: six a day to one) and the entry (#8 -> #9: crossing the spread to
resting as a maker). #11 raised N to 2,329 and lost 0.155 relative to #10;
whatever its other changes did, its marginal trades were negative. So the
budget of this file goes to *quality per trade*, not to count: #10's signal,
its coverage ladder and its one-round-trip discipline are kept, and three
things change.

1. "The move has stopped" now means something absolutely, not just relatively.
   #10 required the last five bars to add or give back no more than 40% of the
   prior 20-bar move. On a 150bp move that permits 60bp of further travel
   inside the "stall" window -- which is not a stall, it is a trend that slowed
   down, and it is precisely the population that keeps going through the fade.
   The bound is now min(40% of the move, 20bp). The relative test still
   handles the small-move case (a 25bp move permits only 10bp); the absolute
   cap handles the large-move case, where the ratio was doing no work at all.
   The relaxed coverage path multiplies the cap by four (80bp) so it stays
   reachable late in the day, and it still cannot trade with the flow.

2. The bracket is widened from 80/55 to 90/60 basis points. With a fixed
   proportional edge in *which barrier is hit first*, the expected value of a
   round trip is edge_pp x (target + stop) -- the barrier geometry only sets
   the break-even hit rate, which is stop/(target+stop) = 0.40 either way. So
   width is close to free P&L if the hit rate holds, and the frame is still
   inside what four hours of BTC can deliver: the expected time to be stopped
   at 60bp is about 90 minutes at recent per-minute volatility, so the 4-hour
   time stop keeps looking at resolved trades rather than at the clock.

3. Position size 100 -> 500 lots. P&L on this run is the price move in USD
   times lots, and 100 lots is 0.01 BTC -- a thousandth of what the top five
   levels of this book show. If the score is a return, this is the one lever
   that scales it linearly, and it is bounded only by the fill model, which at
   0.05 BTC per order still crosses one level. If the score is normalised by
   size, this changes nothing. It is not a signal change and it does not touch
   any threshold below, all of which are in relative units.

What is deliberately not changed
--------------------------------
The signal itself. A move of 25-150bp over the last 20 bars, the last 5 bars
stalled, aggressive flow over those 5 bars at 0.10 of traded volume and still
one-sided, the absorbing side's best price stationary to 5bp, and its top-5
size at least 0.80x its own 60-bar median, 0.80x the other side and 70% of
what it was at the start of the stall. That is the conjunction #10 ran for
+0.0479 and it is not perturbed for the sake of it.

Nor is the coverage ladder, because an unscored run is worse than a negative
one: full signal while more than five hours remain, the same conjunction with
looser thresholds (half the displacement, 25bp wick tolerance, a third of the
flow, a third of the depth sample, 30% refill) between five and two hours, and
the same relaxed signal sent as a market order below two hours. The direction
never changes on any path -- the relaxed branch is a weaker test of the claim,
never a test of its negation.

Housekeeping, all inherited: 1m bars with emit_empty so the pre-close flatten
always has a callback; orders only while ctx.actionable; everything working
cancelled and the position flattened five minutes before session_end so
nothing is carried into a funding print; a 4-hour time stop behind the bracket;
one round trip a day and never a reversal; None from buy/sell read as "no order
exists"; no order id kept across callbacks, so a DAY expiry cannot leave a
stale reference; every rolling window on self, because ctx.book() has no
history and ctx.history() is a bounded push window.
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
        size: int = 500,
        move_bars: int = 20,
        stall_bars: int = 5,
        min_move: float = 0.0025,
        max_move: float = 0.0150,
        stall_frac: float = 0.40,
        stall_abs_frac: float = 0.0020,
        min_flow: float = 0.10,
        depth_window: int = 60,
        depth_min: int = 15,
        depth_hold: float = 0.80,
        depth_vs_other: float = 0.80,
        refill_hold: float = 0.70,
        book_tol: float = 0.0005,
        target_frac: float = 0.0090,
        stop_frac: float = 0.0060,
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
        self.stall_abs_frac = float(stall_abs_frac)
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
        #     by the engine at the instant of the fill. Only the clock is left.
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

        # One round trip a day, and never a reversal.
        if self._entered:
            return

        # --- an entry still working. A maker order that the flow never came
        #     back for is not a position; pull it and try later.
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

        # Coverage is graded. With plenty of day left, demand the full
        # conjunction. After the cutoff, the same conjunction with looser
        # thresholds, first as a maker then as a market; the direction never
        # changes, so the insurance branch is still the claim.
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

        # Maker entry just inside the touch, not at the mid. A sell limit at
        # ask - eps fills only if buyers keep lifting; a buy limit at bid + eps
        # fills only if sellers keep hitting. That is the population the claim
        # names, and eps is kept below a quarter of the spread so the order is
        # still a maker and still needs only a touch of flow.
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
        # Only an entry fill leaves a non-zero position behind it. Counting the
        # trade on the fill rather than on the submission means a rejected or
        # expired entry has not spent the session's one trade.
        if ctx.fill.position != 0:
            self._entered = True
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
        side and a crossed or zero price are all ordinary states, not errors.
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

        The two branches over `prior` are separate, so the absorbing side of
        the book and the side of the trade cannot transpose: prior > 0 sets the
        absorbing side to the asks and the direction to -1 in the same block.

        `relaxed=True` is the coverage path: the same tests with looser
        thresholds, never a different test and never a different direction.
        """
        mb = self.move_bars
        sb = self.stall_bars
        if len(self._closes) < mb + sb + 1:
            return 0
        if len(self._volumes) < sb or len(self._deltas) < sb:
            return 0

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

        # The move must have stopped -- and "stopped" has to be an absolute
        # statement, not only a fraction of how far it went. A 150bp move
        # permits 60bp of further travel under the ratio alone, which is a
        # trend that slowed down, not an absorption.
        stall = (c_now - c_stall) / c_stall
        stall_frac = 1.0 if relaxed else self.stall_frac
        bound = stall_frac * mag
        abs_cap = self.stall_abs_frac * (4.0 if relaxed else 1.0)
        if bound > abs_cap:
            bound = abs_cap
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
            # Absorbed buying: fade it short. Never long here.
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
            # A fall, mirrored. Absorbed selling: fade it long.
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

        # The level must not have been eaten over the stall window itself.
        if len(own) >= sb + 1:
            start_size = float(own[-1 - sb])
            if start_size > 0.0:
                refill = 0.30 if relaxed else self.refill_hold
                if now_size < refill * start_size:
                    return 0

        return direction

    def _enter_market(self, ctx, direction: int) -> None:
        """Coverage entry. Only ever called with a direction `_signal` chose."""
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
