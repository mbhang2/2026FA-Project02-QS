"""Absorption fade on the OKX BTC-USDT perpetual -- ninth attempt: no path
enters with the flow, not even the insurance path.

The claim under test is the campaign's and the direction is theirs. After a
directional move, aggressive flow that runs into displayed liquidity which
*holds* marks a turn: **a short entry follows absorbed buying after a rise, a
long entry follows absorbed selling after a fall.** A trade with the absorbed
flow is testing the opposite claim and is not an implementation of this one.

What the seven scored trials say
--------------------------------
    #0  -0.2927   6,341   target 25 / stop 30     market entry
    #1  -0.1051   6,587   target 50 / stop 35     market entry
    #2  -0.1044   3,054   target 40 / stop 30     mid-limit entry
    #3  -0.1282   4,969   sigma bracket           market entry
    #4  -0.1522   2,237   sigma bracket           mid-limit entry
    #5  not scored: 156/960 sessions exposed, under the 70% floor
    #6  -0.0615   1,189   target 3x / stop        market entry, 1/session
    #7  -0.0901   2,715   target 100 / stop 65    market entry, <=4/session

Score is a sum of per-trade fractional returns, so score ~= N * (edge - cost).
Dividing each trial by its own trade count puts the per-round-trip result at
-4.6, -1.6, -3.4, -2.6, -6.8, ~-5.2 and ~-3.3 basis points. Add back the cost
each execution actually paid and the gross edge lands between -2.8 and +1.6bp,
a distribution centred on zero. #1 is the tightest estimate of it (6,587
market-entry trades) and back-solves to about +1.6bp gross against ~3.2bp of
expected cost.

#6 and #7 already acted on half of that arithmetic by cutting the trade count.
The other half is `cost`, and no earlier file moved it: every one paid 2bp of
taker fee on the entry and another 2bp whenever the trade missed its target.
#2 and #4 rested at the *mid* and were the two worst per-trade results, because
a mid-limit fills only when the price has already come to it, so the 2bp saved
was paid back twice as adverse selection.

That condemns resting *inside* the spread, not resting in general. The entry
here rests **at the touch**, on the side the absorbed flow is leaning into: a
sell limit at the best ask after a rise fills only if buyers keep lifting the
offer, and a buy limit at the best bid after a fall fills only if sellers keep
hitting the bid. That population *is* the population the hypothesis names --
"aggressive buyers met an offer that would not lift" means buyers kept buying.
If they stop, the order does not fill, and not filling is free. Expected cost
falls from ~3.2bp to roughly 1bp: the target leg is a resting limit (0bp on
this run) and only the stop, the time stop and the pre-close flatten pay 2bp.

What this file does, concretely
-------------------------------
  1. **One round trip per session.** #6's lever, kept. ~900 round trips over
     960 sessions rather than 3,000-6,600.
  2. **A maker entry at the touch, not at the mid**, as argued above. An entry
     that has not filled within four bars is cancelled and re-armed later, so a
     level that gets pulled costs nothing.
  3. **The book must hold.** Top-5 displayed size on the absorbing side is at
     least 0.80x its own median over the last 60 samples and at least 0.60x the
     other side's. Both are ratios inside one series, so a change of lot
     convention cannot rescale them; missing or stale rows are skipped rather
     than recorded as zero, so a feed gap cannot read as a collapse in depth.
  4. **Effort without result, over one window.** A 25bp move over the last 20
     bars, the last 5 bars giving back no more than 40% of it and extending it
     by no more than 40%, and aggressive flow still one-sided at 0.10 over
     those same 5 bars (from `bar.delta`, which the bar builder accumulates per
     tick, so no tick subscription and the engine keeps its event skip).
  5. **Coverage insurance that is still the claim.** #5 was not scored at all,
     which is worse than any negative number, so a day that has not filled by
     the cutoff takes a *relaxed* version of the same signal -- a smaller prior
     move, a wider stall tolerance, a smaller flow threshold, a looser depth
     test -- and only if even that fails does the day go untraded. It is not a
     blind fade of the drift. Every entry this file can produce is a short
     after a rise or a long after a fall, gated on flow leaning into a book
     that held; there is no code path that enters with the flow, because that
     would be a different hypothesis.

Direction, stated once and unambiguously
----------------------------------------
`_signal()` returns -1 for **short after absorbed buying** (a rise whose
aggressive buyers met an offer that would not lift) and +1 for **long after
absorbed selling** (a fall whose aggressive sellers met a bid that would not
give way). The two cases are separate branches over `prior`, so the side of the
book that confirms the signal and the side of the trade that takes it cannot be
transposed: `prior > 0.0` sets the absorbing book side to the asks and the
direction to -1 in the same block.

Housekeeping, all established by the earlier files: 1m bars with `emit_empty` so
the pre-close flatten always has a callback; one round trip, never a reversal,
so every trade opens from flat and closes to flat inside its own UTC day;
everything working cancelled and the position flattened five minutes before
`ctx.session_end`, so nothing is carried across a funding print; a 4-hour time
stop; `ctx.actionable` checked before every order; `None` from buy/sell read as
"no order exists"; every rolling window on `self`, because `ctx.book()` has no
history and `ctx.history()` is a bounded push window; no order id kept across
callbacks.
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
        depth_vs_other: float = 0.60,
        target_frac: float = 0.0080,
        stop_frac: float = 0.0055,
        max_spread_frac: float = 0.0012,
        max_hold_bars: int = 240,
        max_attempts: int = 5,
        entry_patience: int = 4,
        retry_cooldown_bars: int = 10,
        flat_margin_s: int = 300,
        signal_cutoff_s: int = 18000,
    ) -> None:
        self.interval = interval
        self.size = max(1, int(size))
        self.move_bars = max(5, int(move_bars))
        self.stall_bars = max(2, int(stall_bars))
        self.min_move = float(min_move)
        self.max_move = float(max_move)
        self.stall_frac = float(stall_frac)
        self.min_flow = float(min_flow)
        self.depth_window = max(10, int(depth_window))
        self.depth_min = max(5, int(depth_min))
        self.depth_hold = float(depth_hold)
        self.depth_vs_other = float(depth_vs_other)
        self.target_frac = float(target_frac)
        self.stop_frac = float(stop_frac)
        self.max_spread_frac = float(max_spread_frac)
        self.max_hold_bars = max(5, int(max_hold_bars))
        self.max_attempts = max(1, int(max_attempts))
        self.entry_patience = max(1, int(entry_patience))
        self.retry_cooldown_bars = max(0, int(retry_cooldown_bars))

        self.flat_margin_ns = max(60, int(flat_margin_s)) * NS_PER_SEC
        self.cutoff_ns = max(3600, int(signal_cutoff_s)) * NS_PER_SEC

        # Everything compared over time lives here.  ctx.history() is a bounded
        # push window and ctx.book() carries no history at all.
        cap = self.move_bars + self.stall_bars + 16
        self._closes: deque = deque(maxlen=cap)
        self._highs: deque = deque(maxlen=cap)
        self._lows: deque = deque(maxlen=cap)
        self._deltas: deque = deque(maxlen=cap)
        self._volumes: deque = deque(maxlen=cap)

        book_cap = self.depth_window + 24
        self._ask_depth: deque = deque(maxlen=book_cap)
        self._bid_depth: deque = deque(maxlen=book_cap)

        self._session = None
        self._entered = False
        self._attempts = 0
        self._pending = 0
        self._cooldown = 0
        self._hold_bars = 0
        self._fallback_done = False

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
            self._fallback_done = False

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
            self._closes.append(float(bar.close))
            self._highs.append(float(bar.high))
            self._lows.append(float(bar.low))
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

        # One round trip a day, and never a reversal.
        if self._entered:
            return

        # --- coverage insurance.  A session with no exposure cannot be scored
        #     at all (#5 traded 156 of 960 and was refused), which is worse than
        #     any negative number.  This stays inside the claim: it is the same
        #     fade, with every threshold relaxed, so it can still only short a
        #     rise or buy a fall after flow leaned into a book that held.
        if (not self._fallback_done and not ctx.working_order_ids
                and remaining <= self.cutoff_ns
                and remaining > self.flat_margin_ns + 3600 * NS_PER_SEC):
            self._fallback_done = True
            direction = self._signal(relaxed=True)
            if direction != 0:
                self._enter_market(ctx, direction)
            return

        if remaining < self.cutoff_ns:
            return

        # --- an entry that has not filled yet.  A resting limit that the flow
        #     never came back for is not a position; pull it and try later.
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

        # Maker entry at the touch.  A sell at the best ask fills only if
        # buyers keep lifting the offer -- which is exactly the flow the claim
        # says is trapped.  If they stop, the order does not fill, and not
        # filling costs nothing.
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

    # --------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        # The first fill that leaves a position behind it is the entry.
        if ctx.fill.position != 0:
            self._entered = True
            self._hold_bars = 0

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
            ask_depth = float(sum(float(x) for x in list(book.ask_sizes)[:5]))
            bid_depth = float(sum(float(x) for x in list(book.bid_sizes)[:5]))
            if ask_depth <= 0.0 or bid_depth <= 0.0:
                return None
            return ask, bid, ask_depth, bid_depth
        except Exception:
            return None

    def _signal(self, relaxed: bool = False) -> int:
        """-1 to short absorbed buying, +1 to buy absorbed selling, 0 for none.

        Effort without result, over one window and at one time: aggressive flow
        strongly one-sided in the direction of a completed move, the price no
        longer extending, and the displayed size on the side the flow ran into
        still there.

        `relaxed=True` is the coverage path: the same tests with every
        threshold loosened, never a different test and never a different
        direction.  The claim is a statement about which way to trade, and a
        version of it that let the insurance branch trade with the flow would
        not be a weaker test of the claim, it would be a test of its negation.
        """
        mb = self.move_bars
        sb = self.stall_bars
        if len(self._closes) < mb + sb + 1:
            return 0

        closes = list(self._closes)
        deltas = list(self._deltas)
        volumes = list(self._volumes)
        if len(volumes) < sb or len(deltas) < sb:
            return 0

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
        # collapsing back through itself -- that is a later, different trade.
        stall = (c_now - c_stall) / c_stall
        stall_frac = 1.0 if relaxed else self.stall_frac
        bound = stall_frac * mag
        if stall > bound or stall < -bound:
            return 0

        # Aggressive flow still leaning the way the move went: buyers still
        # lifting into an offer that will not yield, or sellers still hitting a
        # bid that will not fall.  Normalised by traded volume, not fitted.
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
            own = self._ask_depth        # the offer that absorbed the lifting
            other = self._bid_depth
            direction = -1
        else:
            # A fall whose selling met a bid that would not give way.
            # Absorbed selling: fade it long.  Never short here.
            if flow > -min_flow:
                return 0
            own = self._bid_depth        # the bid that absorbed the hitting
            other = self._ask_depth
            direction = 1

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
        # A level that survived the flow rather than being eaten.
        if now_size < self.depth_hold * hold_mult * reference:
            return 0
        # Supply comparable to demand: not a thin book being swept.
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
