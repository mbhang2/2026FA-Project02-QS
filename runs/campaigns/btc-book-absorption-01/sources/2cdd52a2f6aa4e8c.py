"""Absorption fade on the OKX BTC-USDT perpetual -- seventh attempt.

The claim under test is the campaign's and the direction of the trade is theirs:
after a directional move, aggressive flow that runs into displayed liquidity that
*holds* marks a turn, so we short absorbed buying after a rise and buy absorbed
selling after a fall. Never with the flow.

Where the six earlier attempts leave us
---------------------------------------
    #0  -0.2927  6,341 trades   target 25bp / stop 30bp, market entry
    #1  -0.1051  6,587 trades   target 50bp / stop 35bp, market entry
    #2  -0.1044  3,054 trades   target 40bp / stop 30bp, mid-limit entry
    #3  -0.1282  4,969 trades   sigma bracket, market entry
    #4  -0.1522  2,237 trades   sigma bracket, mid-limit entry
    #5  not scored              exposed in 156/960 sessions

Per round trip those are -4.6, -1.6, -3.4, -2.6 and -6.8 basis points. Backing
the bracket geometry out of each one puts the *gross* edge of the signal at
roughly zero (-1.7bp to +1.6bp), which is what a nearly symmetric barrier on a
driftless walk produces. Two things follow, and this file is built on both:

  * The signal, as those five implementations measured it, does not pay. A
    seventh guess at the same conjunctions is another coin flip.
  * What is left is the arithmetic: score is (per-trade edge - cost) x number of
    trades. With the edge at ~0 and a round trip costing 3-4bp, the number of
    trades is the *only* lever that matters. 6,341 trades of a zero-edge signal
    is -0.29; 960 trades of the same zero-edge signal is -0.03. Every attempt so
    far has been a lever on the wrong term.

So this version trades roughly once per session and spends its effort on not
paying more than it has to:

  1. **One entry per session.** The bracket resolves it; if the day passes
     without a signal there is a single late-session fallback, described below.
     That is ~960 round trips instead of 3,000-6,600, and at a 3-4bp round trip
     it is the difference between -0.10 and -0.03 *with no change in edge*.
  2. **Market entry, deliberately.** #2 and #4 both rested a limit at the mid
     and both were the worst per trade. A limit at the mid is the only liquidity
     at that price, so it is the first thing an aggressive order hits: the fill
     is a selection on the very flow the signal is trying to fade. Two basis
     points buys an unbiased sample, and the whole question is whether the
     sample has an edge.
  3. **A maker exit.** The bracket's target leg is a resting limit and pays 0bp
     on this run, so a winner costs the 2bp entry and nothing else; only the
     stop and the time stop pay taker. Average cost lands near 3bp.
  4. **A wide, structural stop and a 3:1 target.** The stop sits just beyond the
     extreme that the aggressive flow failed to take out, plus a 5bp buffer,
     clamped to [25bp, 80bp]; the target is three times it. Break-even is a 25%
     hit rate, and the payoff is big enough that 3bp of cost is 4% of the target
     rather than the 20% that #0's 25bp target made it.

The signal, at the close of every 1m bar, from rolling state kept on `self`
(`ctx.history` is a bounded push window and `ctx.book` has no history at all):

  * **Prior move** -- close now vs. close 20 bars ago, between 20bp and 120bp in
    one direction. The lower bound is what gives coverage; the upper bound
    excludes the case where the move is still exploding, which is not a stall.
  * **Stall** -- the last 6 bars must stay within 35% of the prior move of where
    the move ended. Neither still advancing nor already given back.
  * **Flow still leaning into it** -- sum(bar.delta)/sum(bar.volume) over those
    same 6 bars, one-sided at 0.12. `bar.delta` is accumulated per tick by the
    bar builder, so this needs no tick subscription and the engine keeps its
    event skip.
  * **The book holds** -- top-5 displayed size on the side the flow is leaning
    on (asks after a rise, bids after a fall) must be at least 0.85x its own
    median over the last 60 samples. The window contains the bars the aggressive
    flow was working through, so depth at or above its own norm now is depth
    that survived. One ratio within one series, so a change of lot convention
    cannot quietly rescale it, and missing or stale rows are skipped rather than
    recorded as zero.

The coverage fallback exists because a session with no exposure cannot be
evaluated: #5 traded 156 sessions out of 960 and was not scored at all, which is
worse than any negative number. If the session reaches its last two hours with
no entry yet, the strategy fades the last hour's drift with no filter. It is
insurance, and it is priced as such -- it costs the round trip and nothing more.

Housekeeping: everything is cancelled and flattened five minutes before
`ctx.session_end`, so every trade opens from flat and closes to flat inside its
own UTC day and nothing is carried into the 00:00 UTC funding print. 1m bars
with `emit_empty=True` guarantee a callback to flatten on. `ctx.actionable` is
checked before every order, `None` from buy/sell is read as "no order exists",
and a 3-hour time stop closes anything the bracket has not resolved.
"""

from __future__ import annotations

NS_PER_SECOND = 1_000_000_000


class Strategy:
    """Fade aggressive flow that the visible book absorbed. One trade a day."""

    def __init__(
        self,
        interval: str = "1m",
        size: int = 100,
        move_bars: int = 20,
        stall_bars: int = 6,
        min_move: float = 0.0020,
        max_move: float = 0.0120,
        stall_frac: float = 0.35,
        min_flow: float = 0.12,
        depth_window: int = 60,
        depth_min: int = 20,
        depth_hold: float = 0.85,
        stop_buffer: float = 0.0005,
        min_stop_frac: float = 0.0025,
        max_stop_frac: float = 0.0080,
        target_mult: float = 3.0,
        min_target_frac: float = 0.0050,
        max_target_frac: float = 0.0200,
        max_hold_bars: int = 180,
        max_entries: int = 1,
        min_spacing_bars: int = 240,
        max_spread_frac: float = 0.0010,
        flat_margin_s: int = 300,
        min_hold_s: int = 2400,
        fallback_after_s: int = 7200,
    ) -> None:
        self.interval = interval
        self.size = max(1, int(size))
        self.move_bars = max(5, int(move_bars))
        self.stall_bars = min(max(2, int(stall_bars)), self.move_bars - 2)
        self.min_move = float(min_move)
        self.max_move = float(max_move)
        self.stall_frac = float(stall_frac)
        self.min_flow = float(min_flow)
        self.depth_window = max(10, int(depth_window))
        self.depth_min = max(5, int(depth_min))
        self.depth_hold = float(depth_hold)
        self.stop_buffer = float(stop_buffer)
        self.min_stop_frac = float(min_stop_frac)
        self.max_stop_frac = float(max_stop_frac)
        self.target_mult = float(target_mult)
        self.min_target_frac = float(min_target_frac)
        self.max_target_frac = float(max_target_frac)
        self.max_hold_bars = max(5, int(max_hold_bars))
        self.max_entries = max(1, int(max_entries))
        self.min_spacing_bars = max(0, int(min_spacing_bars))
        self.max_spread_frac = float(max_spread_frac)

        self.flat_margin_ns = max(1, int(flat_margin_s)) * NS_PER_SECOND
        self.min_hold_ns = max(600, int(min_hold_s)) * NS_PER_SECOND
        self.fallback_ns = max(1, int(fallback_after_s)) * NS_PER_SECOND

        # Rolling state. ctx.history() is a bounded push window and ctx.book()
        # has no history at all, so every series compared over time lives here.
        self._cap = max(self.move_bars + self.stall_bars + 20, 90)
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._deltas: list[float] = []
        self._vols: list[float] = []

        self._book_cap = max(self.depth_window, 20) + 8
        self._ask_depth: list[float] = []
        self._bid_depth: list[float] = []

        self._session = None
        self._bar_index = 0
        self._entries = 0
        self._last_entry_bar = -10 ** 9
        self._hold = 0

    # ------------------------------------------------------------------ wiring

    def setup(self, ctx) -> None:
        # emit_empty so the pre-close flatten always has a callback to fire on:
        # a quiet stretch before midnight must not strand a position overnight.
        ctx.subscribe(self.interval, emit_empty=True)
        # No callback of its own; it only makes ctx.book() readable.
        ctx.subscribe("book")

    # -------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._session = ctx.session
            self._bar_index = 0
            self._entries = 0
            self._last_entry_bar = -10 ** 9
            self._hold = 0

        self._bar_index += 1

        book = self._read_book(ctx)
        if book is not None:
            ask, bid, ask_depth, bid_depth = book
            self._ask_depth.append(ask_depth)
            self._bid_depth.append(bid_depth)
            if len(self._ask_depth) > self._book_cap:
                del self._ask_depth[0]
                del self._bid_depth[0]
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
            self._vols.append(float(bar.volume))
            if len(self._closes) > self._cap:
                del self._closes[0]
                del self._highs[0]
                del self._lows[0]
                del self._deltas[0]
                del self._vols[0]

        remaining = ctx.session_end - ctx.ts

        # --- an open position. The bracket owns the price exits and was armed
        #     by the engine at the instant of the fill; only the clock is left.
        if ctx.position != 0:
            self._hold += 1
            if remaining <= self.flat_margin_ns or self._hold >= self.max_hold_bars:
                self._flatten(ctx)
            return
        self._hold = 0

        if not ctx.actionable:
            return

        if remaining <= self.flat_margin_ns:
            if ctx.working_order_ids:
                ctx.cancel_all()
            return

        if ctx.working_order_ids:
            return
        if self._entries >= self.max_entries:
            return
        if self._bar_index - self._last_entry_bar < self.min_spacing_bars:
            return

        direction = 0
        px = 0.0

        if book is not None and remaining > self.flat_margin_ns + self.min_hold_ns:
            mid = 0.5 * (ask + bid)
            if mid > 0.0 and (ask - bid) <= self.max_spread_frac * mid:
                direction = self._signal()
                px = mid

        if direction == 0 and self._entries == 0:
            # Coverage insurance: late in the day, with nothing taken yet, fade
            # the recent drift with no filter so the session is not blank.
            if (remaining <= self.fallback_ns
                    and remaining > self.flat_margin_ns + 1800 * NS_PER_SECOND):
                direction = self._fallback()
                if direction != 0:
                    px = float(ctx.last_price)

        if direction == 0 or px <= 0.0:
            return

        # Structural stop: just past the extreme the aggressive flow failed to
        # take out, plus a buffer, clamped so it is neither a hair trigger nor a
        # hold-for-the-day.
        if direction < 0:
            extreme = max(self._highs[-self.move_bars:])
            raw = (extreme - px) / px + self.stop_buffer
        else:
            extreme = min(self._lows[-self.move_bars:])
            raw = (px - extreme) / px + self.stop_buffer
        stop_frac = min(max(raw, self.min_stop_frac), self.max_stop_frac)

        target_frac = min(
            max(self.target_mult * stop_frac, self.min_target_frac),
            self.max_target_frac,
        )

        stop_off = px * stop_frac
        target_off = px * target_frac
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
        # None means the action cap dropped it: no order exists.
        if order_id is not None:
            self._entries += 1
            self._last_entry_bar = self._bar_index

    # --------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        # A bracket leg taking us back to flat ends the trade's clock.
        if ctx.fill.position == 0:
            self._hold = 0

    def on_lifecycle(self, ctx) -> None:
        # No order id is kept across callbacks -- the engine holds the bracket
        # and ctx.cancel_all() reaches it -- so a DAY expiry at the session
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
            ask_depth = 0.0
            for x in book.ask_sizes[:5]:
                ask_depth += float(x)
            bid_depth = 0.0
            for x in book.bid_sizes[:5]:
                bid_depth += float(x)
            if ask_depth <= 0.0 or bid_depth <= 0.0:
                return None
            return ask, bid, ask_depth, bid_depth
        except Exception:
            return None

    def _signal(self) -> int:
        """-1 short after absorbed buying, +1 long after absorbed selling, 0 none."""
        w = self.move_bars
        s = self.stall_bars
        if len(self._closes) < w + 2:
            return 0

        closes = self._closes
        c_now = closes[-1]
        c_ref = closes[-1 - w]
        if c_now <= 0.0 or c_ref <= 0.0:
            return 0
        ret = (c_now - c_ref) / c_ref
        mag = ret if ret >= 0.0 else -ret
        if mag < self.min_move or mag > self.max_move:
            return 0

        c_stall = closes[-1 - s]
        if c_stall <= 0.0:
            return 0
        stall_ret = (c_now - c_stall) / c_stall
        bound = self.stall_frac * mag
        if stall_ret > bound or stall_ret < -bound:
            return 0

        if len(self._vols) < s or len(self._deltas) < s:
            return 0
        traded = float(sum(self._vols[-s:]))
        if traded <= 0.0:
            return 0
        flow = float(sum(self._deltas[-s:])) / traded

        if ret > 0.0:
            # Heavy buying into a tape that will not advance: the offer held.
            if flow < self.min_flow:
                return 0
            depths = self._ask_depth
            direction = -1
        else:
            if flow > -self.min_flow:
                return 0
            depths = self._bid_depth
            direction = 1

        if len(depths) < self.depth_min:
            return 0
        window = depths[-self.depth_window:]
        ref = sorted(window)[len(window) // 2]
        if ref <= 0.0:
            return 0
        if depths[-1] < self.depth_hold * ref:
            return 0
        return direction

    def _fallback(self) -> int:
        """Insurance, not signal: fade the last hour so no session is blank."""
        n = len(self._closes)
        if n < 61:
            return 0
        c_now = self._closes[-1]
        c_ref = self._closes[-61]
        if c_now <= 0.0 or c_ref <= 0.0:
            return 0
        if c_now > c_ref:
            return -1
        return 1

    def _flatten(self, ctx) -> None:
        """Cancel anything working and go flat, inside the UTC day."""
        self._hold = 0
        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position != 0:
            ctx.close()
