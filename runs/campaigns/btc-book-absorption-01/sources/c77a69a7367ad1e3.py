"""Absorption fade on OKX BTC-USDT, entered passively at the mid.

The claim under test: after a directional move, aggressive flow that runs into
displayed liquidity which *holds* marks a turn rather than a continuation. This
is the third attempt in the campaign; the two before it lost -0.29 and -0.11
with 6,300-6,600 trades each, which is roughly the number of trades at which a
4 basis point round trip by market order is the entire result. So the first
thing this file changes is not the signal, it is the *price paid* for it.

What changed and why:

  1. **Passive entry at the mid.** The two previous versions sent market orders
     and paid 2bp plus the half-spread to get in. A resting limit at the mid
     pays the maker rate (0bp on this run) and crosses no spread; it sits inside
     the touch with no displayed size ahead of it, so it fills on the next trade
     that prints through the mid -- which on BTC is most trades. Against a 40bp
     target, 2bp of entry cost is 5% of the move being captured, and that is the
     difference between a signal that pays and one that does not. The bracket's
     target leg was already a maker limit (0bp); only the stop and the day-end
     flatten pay taker.
  2. **Fewer, wider.** Target 40bp / stop 30bp with a 25-minute time stop,
     instead of 25bp/30bp (attempt #0, the worse of the two) or 50bp/35bp
     (attempt #1). Break-even hit rate is ~44% before costs.
  3. **The book is read, not remembered.** `ctx.book()` returns one row and
     nothing else, so the depth history is on `self`: top-5 size on each side,
     sampled once per bar, compared against its own rolling median. A level
     that is being eaten collapses in that ratio; a level that refills does not.
     That is the "displayed liquidity holds" half of the claim and it is why
     the book subscription exists at all.

Signal, at the close of every 1m bar, from state on `self` only:

  * **Prior move** -- close now vs. close `prior_bars` (15) bars ago, i.e. the
    last 15 minutes, must be >= 25bp in one direction. Absolute threshold
    rather than volatility-scaled: scaling by recent volatility is a second
    fitted surface and attempt #0 (which had it) did worse.
  * **Stall** -- the move must have stopped: the last 2 bars must not have
    added more than 30% of the prior move in the same direction, and must not
    have given back more than 60% of it (already collapsing = too late).
  * **Unusually large flow** -- sum(delta)/sum(volume) over those same 2 bars,
    one-sided at |flow| >= 0.20 (about 60% of the volume was on one side). The
    normalisation is by traded volume, not fitted. `bar.delta` is accumulated
    per tick by the bar builder, so no tick subscription is needed and the
    engine keeps its event skip.
  * **Liquidity holds** -- the *absorbing* side (ask after a rise, bid after a
    fall) must have top-5 displayed size at least 1.20x its own median over the
    last 40 samples. The window contains the bars in which the aggressive flow
    was working, so depth above its own norm now is depth that survived the
    flow.

Direction is always the fade: short absorbed buying after a rise, long
absorbed selling after a fall. Never with the flow.

Execution details that matter:

  * The limit entry is cancelled after 3 bars if unfilled -- a stale resting
    order in a moved market is an unprotected position waiting to happen.
  * At most 4 filled entries per session and 12 attempted orders, at least 4
    bars apart. Well under the 64-action cap.
  * Five minutes before `ctx.session_end` everything is cancelled and the
    position is flattened at market. `emit_empty=True` guarantees a callback to
    do it on. Every trade therefore opens from flat and closes to flat inside
    its own UTC day, and no position is ever carried across 00:00 UTC funding.
  * `ctx.actionable` is checked before every order; `None` from buy/sell is
    treated as "no order exists".
"""

from __future__ import annotations

from collections import deque

NS_PER_SECOND = 1_000_000_000


class Strategy:
    """Fade aggressive flow that the visible book absorbed, entered passively."""

    def __init__(
        self,
        interval: str = "1m",
        size: int = 100,
        prior_bars: int = 15,
        recent_bars: int = 2,
        min_prior_move: float = 0.0025,
        min_flow: float = 0.20,
        stall_frac: float = 0.30,
        depth_mult: float = 1.20,
        depth_window: int = 40,
        depth_min_samples: int = 15,
        target_frac: float = 0.0040,
        stop_frac: float = 0.0030,
        max_spread_frac: float = 0.0008,
        max_hold_bars: int = 25,
        entry_patience: int = 3,
        max_entries: int = 4,
        max_attempts: int = 12,
        min_spacing_bars: int = 4,
        flat_margin_s: int = 300,
        min_time_to_close_s: int = 1800,
    ) -> None:
        self.interval = interval
        self.size = max(1, int(size))
        self.prior_bars = max(3, int(prior_bars))
        self.recent_bars = max(1, int(recent_bars))
        self.min_prior_move = float(min_prior_move)
        self.min_flow = float(min_flow)
        self.stall_frac = float(stall_frac)
        self.depth_mult = float(depth_mult)
        self.depth_window = max(5, int(depth_window))
        self.depth_min_samples = max(3, int(depth_min_samples))
        self.target_frac = float(target_frac)
        self.stop_frac = float(stop_frac)
        self.max_spread_frac = float(max_spread_frac)
        self.max_hold_bars = max(1, int(max_hold_bars))
        self.entry_patience = max(1, int(entry_patience))
        self.max_entries = max(1, int(max_entries))
        self.max_attempts = max(1, int(max_attempts))
        self.min_spacing_bars = max(0, int(min_spacing_bars))
        self.flat_margin_s = int(flat_margin_s)
        self.min_time_to_close_s = int(min_time_to_close_s)

        # Rolling state. ctx.history is a bounded window and the book has no
        # history at all, so everything the signal reads is kept here.
        cap = self.prior_bars + self.recent_bars + 16
        self._closes: deque[float] = deque(maxlen=cap)
        self._deltas: deque[float] = deque(maxlen=cap)
        self._vols: deque[float] = deque(maxlen=cap)
        self._ask_depth: deque[float] = deque(maxlen=self.depth_window)
        self._bid_depth: deque[float] = deque(maxlen=self.depth_window)

        self._session = None
        self._entries_today = 0
        self._attempts_today = 0
        self._bars_since_entry = 10 ** 9
        self._pending_age = 0
        self._hold_bars = 0

    # ------------------------------------------------------------------ wiring

    def setup(self, ctx) -> None:
        # emit_empty so the pre-close flatten always has a callback to fire on:
        # a quiet hour before midnight must not strand a position overnight.
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
            self._entries_today = 0
            self._attempts_today = 0
            self._bars_since_entry = 10 ** 9
            self._pending_age = 0
            self._hold_bars = 0

        if self._bars_since_entry < 10 ** 9:
            self._bars_since_entry += 1

        book = self._book_state(ctx)
        if book is not None:
            ask, bid, ask_depth, bid_depth = book
            self._ask_depth.append(ask_depth)
            self._bid_depth.append(bid_depth)
        else:
            ask = bid = 0.0

        # Empty bars carry the previous close forward; they advance the clocks
        # but they are not evidence about price or flow.
        if bar.tick_count > 0:
            self._closes.append(float(bar.close))
            self._deltas.append(float(bar.delta))
            self._vols.append(float(bar.volume))

        remaining = ctx.session_end - ctx.ts
        if remaining <= self.flat_margin_s * NS_PER_SECOND:
            self._flatten(ctx)
            return

        if not ctx.actionable:
            return

        # --- an open position: only the time stop is left, the bracket owns
        #     the price exits and was armed at the instant of the fill.
        if ctx.position != 0:
            self._hold_bars += 1
            if self._hold_bars > self.max_hold_bars:
                self._flatten(ctx)
            return
        self._hold_bars = 0

        # --- a resting entry that has not filled yet
        if ctx.working_order_ids:
            self._pending_age += 1
            if self._pending_age > self.entry_patience:
                ctx.cancel_all()
                self._pending_age = 0
            return
        self._pending_age = 0

        if ask <= 0.0 or bid <= 0.0 or ask <= bid:
            return
        if self._entries_today >= self.max_entries:
            return
        if self._attempts_today >= self.max_attempts:
            return
        if self._bars_since_entry < self.min_spacing_bars:
            return
        if remaining < self.min_time_to_close_s * NS_PER_SECOND:
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

        # A resting limit at the mid: inside the touch, nothing displayed ahead
        # of it, maker on both the entry and the target leg.
        if direction > 0:
            order_id = ctx.buy(
                size=self.size,
                type="limit",
                limit=midpoint,
                stop_offset=stop_off,
                target_offset=target_off,
                tif="DAY",
                tag="absorb-long",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                type="limit",
                limit=midpoint,
                stop_offset=stop_off,
                target_offset=target_off,
                tif="DAY",
                tag="absorb-short",
            )
        if order_id is not None:
            self._attempts_today += 1
            self._pending_age = 0

    # --------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        # An entry fill is the only fill that leaves a non-zero position.
        if ctx.fill.position != 0:
            self._entries_today += 1
            self._bars_since_entry = 0
            self._hold_bars = 0
            self._pending_age = 0

    def on_lifecycle(self, ctx) -> None:
        pass

    # ----------------------------------------------------------------- helpers

    def _book_state(self, ctx):
        """(ask, bid, top5 ask size, top5 bid size) or None.

        A missing row, a stale row and an empty side are all ordinary states.
        """
        try:
            book = ctx.book()
            if book is None:
                return None
            ask_prices = book.ask_prices
            bid_prices = book.bid_prices
            if not ask_prices or not bid_prices:
                return None
            ask = float(ask_prices[0])
            bid = float(bid_prices[0])
            if ask <= 0.0 or bid <= 0.0 or ask <= bid:
                return None
            ask_depth = float(sum(book.ask_sizes[:5]))
            bid_depth = float(sum(book.bid_sizes[:5]))
            return ask, bid, ask_depth, bid_depth
        except Exception:
            return None

    def _signal(self) -> int:
        """-1 short after absorbed buying, +1 long after absorbed selling, 0 no."""
        n = len(self._closes)
        need = self.prior_bars + self.recent_bars + 2
        if n < need:
            return 0

        closes = list(self._closes)
        r = self.recent_bars
        c_now = closes[-1]
        c_mid = closes[-1 - r]
        c_old = closes[-1 - r - self.prior_bars]
        if c_old <= 0.0 or c_mid <= 0.0:
            return 0

        move = (c_mid - c_old) / c_old
        if abs(move) < self.min_prior_move:
            return 0

        recent = (c_now - c_mid) / c_mid
        if move > 0.0:
            # Rose, but stopped rising: not still advancing, not yet collapsing.
            if recent > self.stall_frac * move:
                return 0
            if recent < -0.60 * move:
                return 0
        else:
            if recent < self.stall_frac * move:
                return 0
            if recent > -0.60 * move:
                return 0

        vols = list(self._vols)
        deltas = list(self._deltas)
        if len(vols) < r:
            return 0
        volume = 0.0
        for v in vols[-r:]:
            volume += v
        if volume <= 0.0:
            return 0
        delta = 0.0
        for d in deltas[-r:]:
            delta += d
        flow = delta / volume

        if move > 0.0:
            # Absorbed buying: the offer held against the lift. Fade it short.
            if flow < self.min_flow:
                return 0
            depths = self._ask_depth
            direction = -1
        else:
            if flow > -self.min_flow:
                return 0
            depths = self._bid_depth
            direction = 1

        if len(depths) < self.depth_min_samples:
            return 0
        window = list(depths)[-self.depth_window:]
        ref = sorted(window)[len(window) // 2]
        if ref <= 0.0:
            return 0
        if depths[-1] < self.depth_mult * ref:
            return 0
        return direction

    def _flatten(self, ctx) -> None:
        """Cancel anything still working and go flat before the UTC day turns."""
        self._hold_bars = 0
        self._pending_age = 0
        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position != 0:
            ctx.close()
