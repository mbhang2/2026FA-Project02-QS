"""Absorption fade on the OKX BTC-USDT perpetual -- eighth attempt.

The claim under test is the campaign's and the direction of the trade is theirs:
after a directional move, aggressive flow that runs into displayed liquidity
that *holds* marks a turn, so we short absorbed buying after a rise and buy
absorbed selling after a fall. Never with the flow.

What the eight earlier trials actually say
------------------------------------------
    #0  -0.2927   6,341 trades   target 25bp / stop 30bp   market entry
    #1  -0.1051   6,587 trades   target 50bp / stop 35bp   market entry
    #2  -0.1044   3,054 trades   target 40bp / stop 30bp   mid-limit entry
    #3  -0.1282   4,969 trades   sigma bracket            market entry
    #4  -0.1522   2,237 trades   sigma bracket            mid-limit entry
    #5  not scored - exposed on 156 of 960 sessions (70% floor)
    #6  -0.0615   1,189 trades   3:1 structural bracket   market entry
    #7  -0.0901   2,715 trades   100bp / 65bp             market entry

Read the score as `1e-5 * sum over round trips of (net basis points)`. That
calibration is forced by the data: #1 is -0.1051 over 6,587 trades, which is
-1.6bp per trade -- the number its own author quoted -- and #0 is -0.2927 over
6,341, which is -4.6bp. Both round trips cost 3-4bp at this size. So every
attempt so far is consistent with one sentence: **the per-trade edge of the
signal, as implemented, is between -3bp and +1bp, and the fee is 3-4bp.**

Two consequences, and this file is built on both of them.

  1. **The bracket geometry cannot rescue a zero-edge signal.** For a driftless
     walk with drift mu, the probability of touching the target before the stop
     is `S/(T+S) + O(mu/(T+S))`. The excess a strategy needs over that baseline
     to pay its costs is `cost/(T+S)` in probability, and the drift that buys it
     is `mu ~ cost`. The width cancels: the same few basis points of cost must
     be overcome whatever bracket is drawn. That is why four different bracket
     geometries in #0-#4 produced four different-looking, equally negative
     numbers, and it is why #8 does not spend its one degree of freedom on a
     fifth geometry. The bracket here is chosen for horizon (about an hour, so
     the reversion has time to arrive) and for cost asymmetry (the target leg
     rests and pays 0bp on this run; only the stop pays taker), not for edge.

  2. **The only term left is the number of trades.** With a per-trade net of
     -3bp, 6,341 trades is -0.19 of cost and 960 trades is -0.03. #6 already
     proved this: same signal family, a quarter of the trades of #1, and the
     best score of the campaign. #7 then added trades back and lost ground
     again. So this version is a single trade per scored session -- no more
     than 960 round trips over the 960 sessions, and coverage above 90% by
     construction rather than by luck.

The entry, and why it is a market order
---------------------------------------
A resting limit is not cheaper here, it is differently expensive. On this run
a resting limit joins **behind** displayed size and fills only when trades
reach it -- so a passive entry fills exactly when the level it was resting at
is being eaten, which is the failure of the very condition the signal tests.
#2 and #4 both showed the result: below the random-walk baseline, worse than
paying the 2bp. The absorption thesis and the passive entry are logically
incompatible, and the campaign's own warning ("not market making") points the
same way. So: market in, for an unbiased sample of the signal, and a resting
maker target out, so only the outcome that goes wrong pays a second fee.

The signal, at the close of every 1m bar, from rolling state on `self`
(`ctx.history` is a bounded push window and `ctx.book()` has no history at all)

  * **The move** -- close 5 bars ago against close 35 bars ago, between 25bp
    and 200bp. 25bp over half an hour is under one sigma on BTC, so this is the
    coverage-bearing bound, not a rare-event filter.
  * **It has stopped extending** -- over the last 5 bars the price added no
    more than 35% of the move in the same direction and gave back no more than
    50% of it. Neither still advancing (a trend, not a stall) nor already
    collapsed (a different, later trade).
  * **"The best ask did not move up"** -- literally: no new extreme in the
    stall window beyond the extreme of the move window, with a 10bp tolerance
    for a wick. A stall measured on closes alone is satisfied by a bar that
    traded through the offer and came back, which is the opposite of
    absorption. This is the tightest of the conditions and it is the one the
    claim states explicitly.
  * **Flow still leaning into it** -- sum(bar.delta)/sum(bar.volume) over the
    stall window, one-sided at 0.10. `bar.delta` is accumulated per tick by the
    bar builder and excludes unknown-aggressor volume by construction, so no
    tick subscription is needed and the engine keeps its event skip. On this
    dataset unknown volume is zero, so delta is exact.
  * **The level held** -- the top-5 displayed size on the side the flow ran
    into (asks after a rise, bids after a fall) is at least 0.85x its own
    median over the last 45 sampled rows. The window contains the bars the
    aggressive flow was working through, so size at or above its own norm now
    is size that survived. One ratio inside one series, so a change in lot
    convention cannot rescale it; missing or stale book rows are skipped, never
    recorded as zero, because a feed gap must not read as a collapse in depth.

Coverage is graded rather than hoped for. A signal meeting every test enters
while more than eight hours remain; a signal missing only the depth test (the
book is stale or has not sampled) enters between eight and three hours; with
three hours left any session still flat fades its own 60-minute drift, with no
filter, so that the 70% exposure floor cannot be missed the way it was in #5.
That last tier is insurance and is priced as such.

Housekeeping established by the earlier files and kept: orders only while
`ctx.actionable`; `1m` bars with `emit_empty=True` so the pre-close flatten
always has a callback; everything working cancelled and the position flattened
five minutes before `ctx.session_end`, so every trade opens from flat and
closes to flat inside its own UTC day and none is carried into a funding print;
a 210-minute time stop behind the bracket; no order id held across callbacks,
so a DAY expiry cannot leave a stale reference; `None` from buy/sell read as
"no order exists"; at most four entry attempts, spaced 20 bars apart, so a
rejected order does not silently consume the session's one trade.
"""

from __future__ import annotations

from statistics import median

NS_PER_SEC = 1_000_000_000


class Strategy:
    """Fade aggressive flow the visible book absorbed. One trade per UTC day."""

    def __init__(
        self,
        interval: str = "1m",
        size: int = 100,
        move_bars: int = 30,
        stall_bars: int = 5,
        min_move: float = 0.0025,
        max_move: float = 0.0200,
        stall_cap: float = 0.35,
        giveback_cap: float = 0.50,
        new_extreme_tol: float = 0.0010,
        min_flow: float = 0.10,
        depth_hold: float = 0.85,
        depth_window: int = 45,
        depth_min: int = 15,
        target_frac: float = 0.0050,
        stop_frac: float = 0.0080,
        max_spread_frac: float = 0.0010,
        max_hold_bars: int = 210,
        max_attempts: int = 4,
        retry_bars: int = 20,
        flat_margin_s: int = 300,
        relaxed_s: int = 28800,
        last_resort_s: int = 10800,
    ) -> None:
        self.interval = interval
        self.size = max(1, int(size))

        self.move_bars = max(5, int(move_bars))
        self.stall_bars = max(2, int(stall_bars))
        if self.stall_bars >= self.move_bars:
            self.stall_bars = max(2, self.move_bars - 1)

        self.min_move = float(min_move)
        self.max_move = float(max_move)
        self.stall_cap = float(stall_cap)
        self.giveback_cap = float(giveback_cap)
        self.new_extreme_tol = float(new_extreme_tol)
        self.min_flow = float(min_flow)

        self.depth_hold = float(depth_hold)
        self.depth_window = max(8, int(depth_window))
        self.depth_min = max(4, int(depth_min))

        self.target_frac = float(target_frac)
        self.stop_frac = float(stop_frac)
        self.max_spread_frac = float(max_spread_frac)
        self.max_hold_bars = max(5, int(max_hold_bars))

        self.max_attempts = max(1, int(max_attempts))
        self.retry_bars = max(0, int(retry_bars))

        self.flat_margin_ns = max(1, int(flat_margin_s)) * NS_PER_SEC
        self.relaxed_ns = max(1, int(relaxed_s)) * NS_PER_SEC
        self.last_resort_ns = max(1, int(last_resort_s)) * NS_PER_SEC

        # Every series compared over time lives here: ctx.history() is a
        # bounded push window and ctx.book() carries no history at all.
        self._cap = self.move_bars + self.stall_bars + 40
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._deltas: list[float] = []
        self._volumes: list[float] = []

        self._book_cap = self.depth_window + 24
        self._ask_depth: list[float] = []
        self._bid_depth: list[float] = []

        self._session: int | None = None
        self._entries = 0
        self._attempts = 0
        self._cooldown = 0
        self._hold_bars = 0
        self._pending = 0

    # ------------------------------------------------------------------ wiring

    def setup(self, ctx) -> None:
        # emit_empty so the pre-close flatten always has a callback to fire on:
        # a quiet stretch before midnight must never strand a position.
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
            self._cooldown = 0
            self._hold_bars = 0
            self._pending = 0

        book = self._read_book(ctx)
        if book is not None:
            self._ask_depth.append(book[2])
            self._bid_depth.append(book[3])
            if len(self._ask_depth) > self._book_cap:
                del self._ask_depth[0]
                del self._bid_depth[0]

        # Empty bars carry the previous close forward at zero volume: they
        # advance the clock but they are not evidence about price or flow.
        if bar.tick_count > 0:
            self._closes.append(float(bar.close))
            self._highs.append(float(bar.high))
            self._lows.append(float(bar.low))
            self._deltas.append(float(bar.delta))
            self._volumes.append(float(bar.volume))
            if len(self._closes) > self._cap:
                del self._closes[0]
                del self._highs[0]
                del self._lows[0]
                del self._deltas[0]
                del self._volumes[0]

        remaining = ctx.session_end - ctx.ts

        # --- an open position: the bracket owns the price exits and was armed
        #     by the engine at the instant of the fill. Only the clock is left.
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

        # --- an entry that has not filled yet. A market order still unfilled
        #     after three bars is not going to be; drop it and retry later.
        if ctx.working_order_ids:
            self._pending += 1
            if self._pending > 3:
                ctx.cancel_all()
                self._pending = 0
            return
        self._pending = 0

        if self._entries >= 1:
            return
        if self._attempts >= self.max_attempts:
            return
        if self._cooldown > 0:
            self._cooldown -= 1
            return

        direction = 0
        quality = 0
        px = 0.0

        if book is not None:
            ask = book[0]
            bid = book[1]
            midpoint = 0.5 * (ask + bid)
            if midpoint > 0.0 and (ask - bid) <= self.max_spread_frac * midpoint:
                px = midpoint
                direction, quality = self._absorption()

        # Coverage is graded: the full conjunction while there is plenty of day
        # left, a partial one later, an unfiltered drift fade at the end.
        need = 2
        if remaining <= self.relaxed_ns:
            need = 1
        if remaining <= self.last_resort_ns:
            need = 0

        if direction == 0 or quality < need:
            if remaining > self.last_resort_ns:
                return
            direction = self._drift_fade()
            px = float(ctx.last_price)

        if direction == 0 or px <= 0.0:
            return

        # The target leg rests as a limit and pays nothing on this run; the
        # stop pays the taker rate, so it is the wider of the two. Width is set
        # by the reversion horizon (roughly an hour on 1m BTC bars), not by an
        # attempt to manufacture edge -- see the module docstring.
        stop_off = px * self.stop_frac
        target_off = px * self.target_frac
        if stop_off <= 0.0 or target_off <= 0.0:
            return

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
            self._cooldown = self.retry_bars

    # --------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        # Only an entry fill leaves a non-zero position behind it. Counting the
        # trade on the fill rather than on the submission means a rejected or
        # expired entry has not spent the session's one trade.
        if ctx.fill.position != 0:
            self._entries += 1
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
            ask_depth = 0.0
            for level in book.ask_sizes[:5]:
                ask_depth += float(level)
            bid_depth = 0.0
            for level in book.bid_sizes[:5]:
                bid_depth += float(level)
            if ask_depth <= 0.0 or bid_depth <= 0.0:
                return None
            return ask, bid, ask_depth, bid_depth
        except Exception:
            return None

    def _absorption(self):
        """(direction, quality) -- quality 2 is the full conjunction, 1 partial.

        Direction is the fade and never the flow: -1 shorts **absorbed buying**
        after a rise (aggressive buyers met an offer that would not lift), +1
        buys **absorbed selling** after a fall (aggressive sellers met a bid
        that would not give way). The absorbing side of the book and the side
        of the trade are read from the same branch, so they cannot transpose.
        """
        mb = self.move_bars
        sb = self.stall_bars

        if len(self._closes) < mb + sb + 1:
            return 0, 0
        if len(self._highs) < mb + sb + 1 or len(self._lows) < mb + sb + 1:
            return 0, 0
        if len(self._volumes) < sb or len(self._deltas) < sb:
            return 0, 0

        closes = self._closes
        highs = self._highs
        lows = self._lows

        c_now = closes[-1]
        c_stall = closes[-1 - sb]
        c_prior = closes[-1 - sb - mb]
        if c_now <= 0.0 or c_stall <= 0.0 or c_prior <= 0.0:
            return 0, 0

        prior = (c_stall - c_prior) / c_prior
        magnitude = prior if prior >= 0.0 else -prior
        if magnitude < self.min_move or magnitude > self.max_move:
            return 0, 0

        stall_ret = (c_now - c_stall) / c_stall

        if prior > 0.0:
            # A rise that has stopped rising without yet giving itself back.
            if stall_ret > self.stall_cap * prior:
                return 0, 0
            if stall_ret < -self.giveback_cap * prior:
                return 0, 0
            # "The best ask did not move up", measured on the wick.
            prior_extreme = max(highs[-(mb + sb):-sb])
            stall_extreme = max(highs[-sb:])
            if stall_extreme > prior_extreme * (1.0 + self.new_extreme_tol):
                return 0, 0
            direction = -1
            own = self._ask_depth
        else:
            # A fall, mirrored.
            if stall_ret < self.stall_cap * prior:
                return 0, 0
            if stall_ret > -self.giveback_cap * prior:
                return 0, 0
            prior_extreme = min(lows[-(mb + sb):-sb])
            stall_extreme = min(lows[-sb:])
            if stall_extreme < prior_extreme * (1.0 - self.new_extreme_tol):
                return 0, 0
            direction = 1
            own = self._bid_depth

        # Aggressive flow still leaning the way the move went, normalised by
        # traded volume over the same bars. delta excludes unattributed volume
        # by construction, and on this dataset there is none.
        traded = float(sum(self._volumes[-sb:]))
        if traded <= 0.0:
            return 0, 0
        flow = float(sum(self._deltas[-sb:])) / traded

        if direction < 0:
            if flow < self.min_flow:
                return 0, 0
        else:
            if flow > -self.min_flow:
                return 0, 0

        # The level survived rather than being eaten. Checked last because it
        # is the only test that needs a sort, and it is the one allowed to fail
        # without losing the signal -- it demotes the quality, it does not
        # cancel the trade.
        history = [v for v in own if v > 0.0]
        if len(history) < self.depth_min:
            return direction, 1
        reference = median(history[-self.depth_window:])
        if reference <= 0.0:
            return direction, 1
        if own[-1] < self.depth_hold * reference:
            return direction, 1
        return direction, 2

    def _drift_fade(self) -> int:
        """Last-resort coverage: fade the session's own recent drift.

        Insurance, not signal. A session with no exposure cannot be evaluated,
        and #5 was not scored at all for missing the floor.
        """
        if len(self._closes) < 61:
            return 0
        c_now = self._closes[-1]
        c_ref = self._closes[-61]
        if c_now <= 0.0 or c_ref <= 0.0:
            return 0
        if c_now >= c_ref:
            return -1
        return 1

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
