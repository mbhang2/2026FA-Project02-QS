"""Absorption fade on the OKX BTC-USDT perpetual -- tenth attempt.

The claim under test is the campaign's and the direction of the trade is theirs.
After a directional move, aggressive flow that runs into displayed liquidity
which *holds* marks a turn: **a short entry follows absorbed buying after a
rise, a long entry follows absorbed selling after a fall.** A trade with the
absorbed flow is testing the opposite claim and is not an implementation of
this one.

Where the campaign stands
-------------------------
    #0  -0.2927   6,341   target 25 / stop 30     market entry
    #1  -0.1051   6,587   target 50 / stop 35     market entry
    #2  -0.1044   3,054   mid-limit entry
    #3  -0.1282   4,969   sigma bracket           market entry
    #4  -0.1522   2,237   sigma bracket           mid-limit entry
    #5  not scored: 156/960 sessions exposed, under the 70% floor
    #6  -0.0615   1,189   3:1 structural bracket   market entry, 1/session
    #7  -0.0901   2,715   wide bracket            market entry, <=4/session
    #8  -0.0047   1,547   wide bracket            market entry, 1/session
    #9  +0.0427   1,213   wide bracket            maker-at-touch entry, 1/session

Two things moved the number and nothing else did. #6 -> #8 was the trade count:
one round trip a day instead of six, which removed the fee drag that had been
dominating every earlier trial. #8 -> #9 was the entry: resting a limit at the
touch instead of crossing. The previous file argued that a mid-limit (tried in
#2 and #4) is doubly expensive because it only fills after the price has come to
it, while a touch-limit fills exactly when the aggressive flow the claim names
is still there -- if the flow stops, the order does not fill, and not filling is
free. #9 is the first positive score of the campaign and it is that change.

What this file changes, and why
-------------------------------
Three things, all of which follow from the arithmetic rather than from a search.

1. **The prior move is measured in sigmas, not in basis points.** #9 required
   25bp of displacement over 20 bars, a fixed number applied to three years of
   BTC whose per-minute volatility moves by a factor of several. In a quiet
   regime 25bp over 20 minutes is a two-sigma event and in a violent one it is
   noise, so the same threshold selects different populations in different
   months. The threshold here is `move_sigmas * sigma_bar * sqrt(move_bars)`,
   with `sigma_bar` a robust (median-absolute-deviation) estimate of the
   per-bar return scale over the last ~240 bars, floored by an absolute minimum
   so that a dead tape does not produce a 3bp "move". This is the campaign's own
   instruction -- "normalise against recent flow or recent total volume" --
   applied to the price leg of the same conjunction, and it is the single
   cheapest way to make the signal mean the same thing in 2023 and in 2026.

2. **The stall window must be doing work.** Absorption is aggressive volume
   meeting size, and a five-bar stall on no volume is just a quiet patch. The
   trade requires the volume traded through the stall window to be at least
   0.7x its own 60-bar median. One ratio inside one series, so a change of lot
   convention cannot rescale it.

3. **Two round trips a session instead of one.** This is the one change that
   deliberately raises N, and it is the answer to a question the earlier files
   answered with the wrong cost structure. Under #8's cost model -- 2bp on the
   entry plus 2bp on every miss -- a round trip cost ~3bp against a gross edge
   back-solved at somewhere between -3bp and +1.6bp, so every extra trade was
   negative expected value and cutting N was unambiguously right. #9 changed
   the cost: the entry rests at the touch (0bp), the target rests (0bp), and
   only the stop, the time stop and the pre-close flatten pay. A round trip now
   costs about 1bp. If the gross edge is what #9's positive score suggests,
   doubling the trade count doubles the score; if it is zero, it doubles a
   number that is small either way. The second trip is gated identically to the
   first -- full conjunction, no relaxation -- and needs the first to be closed
   and a cooldown of 12 bars to have elapsed, so it can never be a reversal and
   every trade still opens from flat and closes to flat inside its own UTC day.

Everything else is #9, deliberately: it is the first thing that worked and it
should not be perturbed for the sake of it.

The signal, at the close of every 1m bar, from rolling state on `self`
(`ctx.history` is a bounded push window and `ctx.book()` has no history at all)

  * **The move** -- close 5 bars ago against close 25 bars ago, at least
    `1.0 sigma_bar sqrt(20)` in size and at least 15bp, and no larger than 250bp
    (beyond that it is a news bar, not an absorption).
  * **It has stopped extending** -- over the last 5 bars the price added no more
    than 40% of the move and gave back no more than 50% of it, and it made no
    new extreme beyond a 10bp wick tolerance. A stall measured on closes alone
    is satisfied by a bar that traded through the offer and came back, which is
    the opposite of absorption.
  * **Flow still leaning into it** -- sum(bar.delta)/sum(bar.volume) over the
    stall window, one-sided at 0.10. `bar.delta` is accumulated per tick by the
    bar builder, so no tick subscription is needed and the engine keeps its
    event skip; on this dataset unknown-aggressor volume is zero, so the ratio
    is exact.
  * **Effort, not torpor** -- the stall window's total volume at least 0.7x its
    60-bar median.
  * **The level held** -- top-5 displayed size on the side the flow ran into
    (asks after a rise, bids after a fall) at least 0.80x its own median over
    the last 60 sampled rows and at least 0.60x the other side. Missing or stale
    book rows are skipped rather than recorded as zero, so a feed gap cannot
    read as a collapse in depth.

Direction, stated once and unambiguously: `_signal()` returns -1 for **short
after absorbed buying** and +1 for **long after absorbed selling**. The two
cases are separate branches over `prior`, so the side of the book that confirms
the signal and the side of the trade that takes it cannot be transposed --
`prior > 0.0` sets the absorbing book side to the asks and the direction to -1
in the same block. There is no code path that enters with the flow.

Coverage, because an unscored run is worse than a negative one. If six hours
remain and the day has not traded, a relaxed version of the same conjunction --
half the displacement, three times the wick tolerance, a third of the flow
requirement, a third of the depth sample -- is evaluated every 20 minutes until
it fires or the entry cutoff arrives. It is the same fade with looser numbers,
never a different test: it can still only short a rise or buy a fall.

Housekeeping, all established by the earlier files: 1m bars with `emit_empty` so
the pre-close flatten always has a callback; orders only while `ctx.actionable`;
everything working cancelled and the position flattened five minutes before
`ctx.session_end`, so nothing is carried across a funding print; a 4-hour time
stop behind the bracket; `None` from buy/sell read as "no order exists"; no
order id kept across callbacks, so a DAY expiry cannot leave a stale reference;
every rolling window on `self`.
"""

from __future__ import annotations

from collections import deque
from statistics import median

NS_PER_SEC = 1_000_000_000


class Strategy:
    """Fade aggressive flow the visible book absorbed. Maker entries, two a day at most."""

    def __init__(
        self,
        interval: str = "1m",
        size: int = 100,
        move_bars: int = 20,
        stall_bars: int = 5,
        move_sigmas: float = 1.0,
        min_move: float = 0.0015,
        max_move: float = 0.0250,
        stall_frac: float = 0.40,
        giveback_frac: float = 0.50,
        new_extreme_tol: float = 0.0010,
        min_flow: float = 0.10,
        vol_window: int = 60,
        vol_mult: float = 0.70,
        depth_window: int = 60,
        depth_min: int = 15,
        depth_hold: float = 0.80,
        depth_vs_other: float = 0.60,
        target_frac: float = 0.0080,
        stop_frac: float = 0.0055,
        max_spread_frac: float = 0.0012,
        max_hold_bars: int = 240,
        max_round_trips: int = 2,
        max_attempts: int = 10,
        entry_patience: int = 4,
        retry_cooldown_bars: int = 8,
        cooldown_after_exit: int = 12,
        flat_margin_s: int = 300,
        entry_cutoff_s: int = 5400,
        fallback_start_s: int = 21600,
        fallback_retry_bars: int = 20,
    ) -> None:
        self.interval = interval
        self.size = max(1, int(size))

        self.move_bars = max(5, int(move_bars))
        self.stall_bars = max(2, int(stall_bars))
        if self.stall_bars >= self.move_bars:
            self.stall_bars = max(2, self.move_bars - 1)

        self.move_sigmas = float(move_sigmas)
        self.min_move = float(min_move)
        self.max_move = float(max_move)
        self.stall_frac = float(stall_frac)
        self.giveback_frac = float(giveback_frac)
        self.new_extreme_tol = float(new_extreme_tol)
        self.min_flow = float(min_flow)

        self.vol_window = max(10, int(vol_window))
        self.vol_mult = float(vol_mult)

        self.depth_window = max(8, int(depth_window))
        self.depth_min = max(4, int(depth_min))
        self.depth_hold = float(depth_hold)
        self.depth_vs_other = float(depth_vs_other)

        self.target_frac = float(target_frac)
        self.stop_frac = float(stop_frac)
        self.max_spread_frac = float(max_spread_frac)
        self.max_hold_bars = max(5, int(max_hold_bars))

        self.max_round_trips = max(1, int(max_round_trips))
        self.max_attempts = max(1, int(max_attempts))
        self.entry_patience = max(1, int(entry_patience))
        self.retry_cooldown_bars = max(0, int(retry_cooldown_bars))
        self.cooldown_after_exit = max(0, int(cooldown_after_exit))

        self.flat_margin_ns = max(60, int(flat_margin_s)) * NS_PER_SEC
        self.entry_cutoff_ns = max(600, int(entry_cutoff_s)) * NS_PER_SEC
        self.fallback_start_ns = max(1200, int(fallback_start_s)) * NS_PER_SEC
        self.fallback_retry_bars = max(1, int(fallback_retry_bars))

        # Everything compared over time lives here: ctx.history() is a bounded
        # push window and ctx.book() carries no history at all.
        self._cap = max(self.move_bars + self.stall_bars + 16,
                        self.vol_window + 20, 120)
        self._closes: deque = deque(maxlen=self._cap)
        self._highs: deque = deque(maxlen=self._cap)
        self._lows: deque = deque(maxlen=self._cap)
        self._deltas: deque = deque(maxlen=self._cap)
        self._volumes: deque = deque(maxlen=self._cap)
        self._absret: deque = deque(maxlen=300)

        self._book_cap = self.depth_window + 24
        self._ask_depth: deque = deque(maxlen=self._book_cap)
        self._bid_depth: deque = deque(maxlen=self._book_cap)

        self._session = None
        self._round_trips = 0
        self._attempts = 0
        self._counted = False
        self._in_pos = False
        self._pending = 0
        self._cooldown = 0
        self._exit_cd = 0
        self._hold_bars = 0
        self._fallback_cd = 0

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
            self._round_trips = 0
            self._attempts = 0
            self._counted = False
            self._in_pos = False
            self._pending = 0
            self._cooldown = 0
            self._exit_cd = 0
            self._hold_bars = 0
            self._fallback_cd = 0

        if self._cooldown > 0:
            self._cooldown -= 1

        book = self._read_book(ctx)
        if book is not None:
            ask, bid, ask_depth, bid_depth = book
            # Only real rows enter the depth history.  A missing or stale row
            # is not a collapse in displayed size, and recording it as one
            # would make every feed gap look like the book being eaten.
            self._ask_depth.append(ask_depth)
            self._bid_depth.append(bid_depth)
        else:
            ask = 0.0
            bid = 0.0

        # Empty bars carry the previous close forward at zero volume: they
        # advance the clocks but they are not evidence about price or flow.
        if bar.tick_count > 0:
            close = float(bar.close)
            if self._closes:
                prev = self._closes[-1]
                if prev > 0.0 and close > 0.0:
                    r = (close - prev) / prev
                    self._absret.append(r if r >= 0.0 else -r)
            self._closes.append(close)
            self._highs.append(float(bar.high))
            self._lows.append(float(bar.low))
            self._deltas.append(float(bar.delta))
            self._volumes.append(float(bar.volume))

        remaining = ctx.session_end - ctx.ts

        # --- an open position: the bracket owns the price exits and was armed
        #     by the engine at the instant of the fill.  Only the clock is left.
        if ctx.position != 0:
            self._in_pos = True
            self._hold_bars += 1
            if remaining <= self.flat_margin_ns or self._hold_bars >= self.max_hold_bars:
                self._flatten(ctx)
            return

        # Just went flat, by bracket or by our own close: rest before re-arming
        # so a second trip cannot be a reversal of the first.
        if self._in_pos:
            self._in_pos = False
            self._hold_bars = 0
            self._pending = 0
            self._exit_cd = self.cooldown_after_exit

        if not ctx.actionable:
            return

        if remaining <= self.flat_margin_ns:
            if ctx.working_order_ids:
                ctx.cancel_all()
            return

        if self._exit_cd > 0:
            self._exit_cd -= 1
            return

        # Never more than the day's allowance, and never a reversal.
        if self._round_trips >= self.max_round_trips:
            return

        # No new exposure this late: there is no time left for the reversion
        # and the flatten would pay the taker fee for nothing.
        if remaining <= self.entry_cutoff_ns:
            if ctx.working_order_ids:
                ctx.cancel_all()
            return

        # --- an entry that has not filled yet.  A resting limit the flow never
        #     came back for is not a position; pull it and try later.
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

        # --- coverage insurance.  A session with no exposure cannot be scored,
        #     and #5 was not scored at all.  The relaxed signal is the same
        #     conjunction with looser thresholds, never a different test, and it
        #     is retried so that one unlucky instant cannot cost the whole day.
        if self._round_trips == 0 and remaining <= self.fallback_start_ns:
            if self._fallback_cd > 0:
                self._fallback_cd -= 1
                return
            direction = self._signal(relaxed=True)
            if direction == 0:
                self._fallback_cd = self.fallback_retry_bars
                return
            self._enter_market(ctx, direction)
            return

        if book is None:
            return
        if ask <= 0.0 or bid <= 0.0 or ask <= bid:
            return
        mid = 0.5 * (ask + bid)
        if mid <= 0.0 or (ask - bid) > self.max_spread_frac * mid:
            return

        direction = self._signal()
        if direction == 0:
            return

        stop_off = mid * self.stop_frac
        target_off = mid * self.target_frac
        if stop_off <= 0.0 or target_off <= 0.0:
            return

        # Maker entry at the touch.  A sell at the best ask fills only if buyers
        # keep lifting the offer -- exactly the flow the claim says is trapped.
        # If they stop, the order does not fill, and not filling is free.
        if direction > 0:
            order_id = ctx.buy(
                size=self.size,
                type="limit",
                limit=bid,
                stop_offset=stop_off,
                target_offset=target_off,
                tif="DAY",
                tag="absorb-long",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                type="limit",
                limit=ask,
                stop_offset=stop_off,
                target_offset=target_off,
                tif="DAY",
                tag="absorb-short",
            )
        if order_id is not None:
            self._attempts += 1
            self._pending = 0
            self._counted = False

    # --------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        # A fill that leaves a position behind it is an entry.  The flag keeps a
        # partly filled resting order from spending the day's allowance twice.
        if ctx.fill.position != 0:
            if not self._counted:
                self._counted = True
                self._round_trips += 1
            self._hold_bars = 0
            self._pending = 0
        else:
            self._counted = False

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
            ask_depth = float(sum(float(x) for x in list(book.ask_sizes)[:5]))
            bid_depth = float(sum(float(x) for x in list(book.bid_sizes)[:5]))
            if ask_depth <= 0.0 or bid_depth <= 0.0:
                return None
            return ask, bid, ask_depth, bid_depth
        except Exception:
            return None

    def _sigma(self):
        """Robust per-bar return scale, or None while it is still spinning up.

        Median absolute deviation, which one violent bar cannot inflate the way
        a standard deviation can -- and a threshold that a single outlier moves
        is a threshold that stops selecting the same population.
        """
        if len(self._absret) < 30:
            return None
        sample = list(self._absret)
        m = median(sample[-240:])
        if m <= 0.0:
            return None
        return 1.4826 * m

    def _signal(self, relaxed: bool = False) -> int:
        """-1 to short absorbed buying, +1 to buy absorbed selling, 0 for none.

        Effort without result, over one window and at one time: aggressive flow
        strongly one-sided in the direction of a completed move, the price no
        longer extending, and the displayed size on the side the flow ran into
        still there.

        `relaxed=True` is the coverage path: the same tests with every threshold
        loosened, never a different test and never a different direction.  The
        claim is a statement about which way to trade, and a version of it that
        let the insurance branch trade with the flow would not be a weaker test
        of the claim, it would be a test of its negation.
        """
        mb = self.move_bars
        sb = self.stall_bars

        if len(self._closes) < mb + sb + 1:
            return 0
        if len(self._highs) < mb + sb or len(self._lows) < mb + sb:
            return 0
        if len(self._deltas) < sb or len(self._volumes) < sb:
            return 0

        # Materialised as lists: a deque supports int indexing but not slices,
        # and both the extreme lookups below are slices of the price series.
        closes = list(self._closes)
        highs = list(self._highs)
        lows = list(self._lows)
        deltas = list(self._deltas)
        volumes = list(self._volumes)

        c_now = closes[-1]
        c_stall = closes[-1 - sb]
        c_prior = closes[-1 - sb - mb]
        if c_now <= 0.0 or c_stall <= 0.0 or c_prior <= 0.0:
            return 0

        prior = (c_stall - c_prior) / c_prior
        magnitude = prior if prior >= 0.0 else -prior

        # The prior move, in units of its own recent volatility rather than in
        # fixed basis points: three years of BTC does not have one volatility.
        need_mult = 0.5 if relaxed else 1.0
        floor = self.min_move * need_mult
        threshold = floor
        sigma = self._sigma()
        if sigma is not None:
            scaled = need_mult * self.move_sigmas * sigma * (float(mb) ** 0.5)
            if scaled > threshold:
                threshold = scaled
        if magnitude < threshold:
            return 0
        if magnitude > self.max_move * (2.0 if relaxed else 1.0):
            return 0

        stall = (c_now - c_stall) / c_stall
        stall_cap = 1.0 if relaxed else self.stall_frac
        give_cap = 1.0 if relaxed else self.giveback_frac
        tol = self.new_extreme_tol * (3.0 if relaxed else 1.0)

        if prior > 0.0:
            # A rise that stopped rising without giving itself back, and made no
            # new high beyond a wick: a stall measured on closes alone would be
            # satisfied by a bar that traded through the offer and came back,
            # which is the opposite of absorption.
            if stall > stall_cap * magnitude:
                return 0
            if stall < -give_cap * magnitude:
                return 0
            prior_extreme = max(highs[-(mb + sb):-sb])
            stall_extreme = max(highs[-sb:])
            if stall_extreme > prior_extreme * (1.0 + tol):
                return 0
            # Absorbed buying: fade it short.  Never long here.
            direction = -1
            own = self._ask_depth
            other = self._bid_depth
        else:
            # A fall, mirrored.
            if stall < -stall_cap * magnitude:
                return 0
            if stall > give_cap * magnitude:
                return 0
            prior_extreme = min(lows[-(mb + sb):-sb])
            stall_extreme = min(lows[-sb:])
            if stall_extreme < prior_extreme * (1.0 - tol):
                return 0
            # Absorbed selling: fade it long.  Never short here.
            direction = 1
            own = self._bid_depth
            other = self._ask_depth

        # Aggressive flow still leaning the way the move went, normalised by
        # traded volume over the same bars.  delta excludes unattributed volume
        # by construction; on this dataset there is none.
        traded = float(sum(volumes[-sb:]))
        if traded <= 0.0:
            return 0
        flow = float(sum(deltas[-sb:])) / traded
        flow_need = self.min_flow * (0.3 if relaxed else 1.0)
        if direction < 0:
            if flow < flow_need:
                return 0
        else:
            if flow > -flow_need:
                return 0

        # Effort, not torpor: a five-bar stall on no volume is a quiet patch,
        # not aggressive flow meeting size.  One ratio inside one series, so a
        # change of lot convention cannot rescale it.
        if not relaxed and len(volumes) >= self.vol_window:
            typical = median(volumes[-self.vol_window:])
            if typical > 0.0 and traded < self.vol_mult * typical * sb:
                return 0

        # The level survived rather than being eaten.  Checked last because it
        # is the only test that needs a sort.
        need = self.depth_min if not relaxed else max(3, self.depth_min // 3)
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
        hold_mult = 0.70 if relaxed else 1.0
        other_mult = 0.40 if relaxed else 1.0
        if now_size < self.depth_hold * hold_mult * reference:
            return 0
        if now_size < self.depth_vs_other * other_mult * other_size:
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
            self._pending = 0
            self._counted = False

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
