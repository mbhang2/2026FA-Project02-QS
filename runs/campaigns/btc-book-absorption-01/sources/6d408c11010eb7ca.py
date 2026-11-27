"""Absorption fade on the OKX BTC-USDT perpetual, second refinement.

The claim under test is unchanged: after a directional move, aggressive flow that
runs into displayed liquidity which *holds* marks a turn more often than a
continuation. The trade is the fade -- short absorbed buying after a rise, long
absorbed selling after a fall -- never with the flow.

Both prior trials lost, and the interesting question is why. Both were faded at
roughly the same rate (6,500 trades over 960 sessions, ~6.8 a day) with tight
25-35bp brackets against a 2bp taker entry and a 2bp taker stop. At that trade
rate the cost drag is large and the signal is diluted: an 12-20bp move on BTC is
well inside one sigma of a 20-minute move, so most "prior moves" being faded
were noise, and noise faded is noise paid for.

What changes here, and the reasoning for each:

  * **A bigger prior move, a tighter stall.** 20bp over 20 one-minute bars, then
    five bars in which price advances by no more than 25% of the move and gives
    back no more than 40%. The old versions accepted moves half this size and
    allowed the stall to be looser, which is the main source of the trade count.

  * **The absorption test is now about the side facing the flow, at two
    different timescales.** The top-3 displayed size on the side the flow is
    leaning on must be (a) at least 0.85x its own median over the last 30 book
    snapshots -- a wall rather than a thin book -- and (b) at least 0.80x what it
    was `stall_bars` ago, i.e. it was not eaten during the very bars in which the
    aggressive flow was arriving. (b) is the "refills" half of the claim; a level
    that is being consumed fails it and a level that is being refreshed passes.
    Both are ratios within the same series, so a change of lot convention cannot
    quietly rescale the threshold.

  * **The offer must be the heavy side.** For a short, the top-3 ask size must
    be at least the top-3 bid size at the same instant. That is supply stacking
    up against the buyers who are still lifting, and it is a piece of book
    information the delta number cannot contain. Prior versions never compared
    the two sides at all.

  * **The touch-price test is dropped, deliberately.** It was vacuous: the stall
    is defined as price not moving over the last `stall_bars`, and the ask
    necessarily tracks price, so "the offer did not lift" was implied by a
    condition already imposed. It filtered nothing and made the file look like it
    measured something it did not.

  * **Brackets are scaled to the move that is being faded**, target 1.5x the
    prior move and stop 1.0x it, clamped to [20bp, 80bp] and [12bp, 50bp]. A
    fixed 25bp target against an 18bp move was asking for more than the retrace
    the claim implies, and a fixed 30bp stop was wider than the target -- a
    payoff that needs a hit rate the effect will not deliver after costs.

  * **Fewer, more separated trades.** At most 4 entries a session, at least 20
    minutes apart, none in the first 15 minutes of the UTC day (the 00:00 funding
    settlement prints through here) and none in the last 45 minutes. Up to ~3,800
    trades instead of ~6,500, which is where the fee drag was being paid.

Execution: market entry (taker, 2bp) -- a resting bid behind a level that is by
construction holding is the order least likely to fill at exactly the moment the
signal is strongest, which is the adverse-selection corner of a maker entry. The
target leg is the engine's own limit (maker, 0bp). The stop, the 120-minute time
stop and the pre-close flatten are the only other taker exits. One position at a
time, no reversal: a new signal while a position is open is ignored, so every
trade opens from flat and closes to flat inside its own UTC day. Position,
working orders, actionable flag and the action cap are all checked every bar;
each bracket is placed with the entry so protection exists from the instant of
the fill.

No tick subscription: `bar.delta` carries signed aggressive volume at bar
resolution already, `unknown_volume` is zero on this feed so the delta is
complete, and a tick feed costs roughly 5x the wall clock for a sub-bar sequence
this signal does not use. `emit_empty=True` only so the pre-close flatten always
has a callback to fire on.
"""

from __future__ import annotations

from collections import deque
from statistics import median

NS_PER_SEC = 1_000_000_000
NS_PER_MIN = 60 * NS_PER_SEC


class Strategy:
    """Fade aggressive flow that fails against displayed book liquidity."""

    def __init__(
        self,
        interval: str = "1m",
        size: int = 100,
        move_bars: int = 20,
        stall_bars: int = 5,
        min_move: float = 0.0020,
        stall_cap: float = 0.25,
        giveback_cap: float = 0.40,
        min_flow: float = 0.20,
        size_hold: float = 0.85,
        refill_ratio: float = 0.80,
        book_window: int = 30,
        book_min: int = 12,
        target_mult: float = 1.5,
        stop_mult: float = 1.0,
        min_target: float = 0.0020,
        max_target: float = 0.0080,
        min_stop: float = 0.0012,
        max_stop: float = 0.0050,
        max_hold_min: int = 120,
        flat_margin_min: int = 5,
        session_cutoff_min: int = 45,
        open_skip_min: int = 15,
        max_entries: int = 4,
        min_spacing_bars: int = 20,
    ) -> None:
        self.interval = interval
        self.size = max(1, int(size))
        self.move_bars = max(3, int(move_bars))
        self.stall_bars = max(1, int(stall_bars))
        self.min_move = float(min_move)
        self.stall_cap = float(stall_cap)
        self.giveback_cap = float(giveback_cap)
        self.min_flow = float(min_flow)
        self.size_hold = float(size_hold)
        self.refill_ratio = float(refill_ratio)
        self.book_window = max(6, int(book_window))
        self.book_min = max(3, int(book_min))
        self.target_mult = float(target_mult)
        self.stop_mult = float(stop_mult)
        self.min_target = float(min_target)
        self.max_target = float(max_target)
        self.min_stop = float(min_stop)
        self.max_stop = float(max_stop)
        self.max_hold_ns = max(1, int(max_hold_min)) * NS_PER_MIN
        self.flat_margin_ns = max(1, int(flat_margin_min)) * NS_PER_MIN
        self.session_cutoff_ns = max(1, int(session_cutoff_min)) * NS_PER_MIN
        self.open_skip_ns = max(0, int(open_skip_min)) * NS_PER_MIN
        self.max_entries = max(1, int(max_entries))
        self.min_spacing_bars = max(0, int(min_spacing_bars))

        # Rolling state lives here: ctx.history() is a bounded window and the
        # book has no history at all, so anything compared over time is kept.
        self._closes: deque[float] = deque(maxlen=64)
        self._deltas: deque[int] = deque(maxlen=32)
        self._volumes: deque[int] = deque(maxlen=32)
        self._ask_px: deque[float | None] = deque(maxlen=40)
        self._ask_sz: deque[float | None] = deque(maxlen=40)
        self._bid_px: deque[float | None] = deque(maxlen=40)
        self._bid_sz: deque[float | None] = deque(maxlen=40)

        self._session: int | None = None
        self._entries = 0
        self._since_entry = 10 ** 9
        self._entry_ts: int | None = None

    # ------------------------------------------------------------------ wiring

    def setup(self, ctx) -> None:
        # emit_empty so the pre-close flatten always has a callback to fire on.
        ctx.subscribe(self.interval, emit_empty=True)
        # No callback of its own; this only makes ctx.book() readable.
        ctx.subscribe("book")

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        if ctx.session != self._session:
            self._session = ctx.session
            self._entries = 0
            self._since_entry = 10 ** 9
            self._entry_ts = None

        book = self._read_book(ctx)

        # Accumulate on every bar, warmup included: nothing here is fitted, the
        # warmup bars simply prime the rolling windows.
        self._closes.append(float(bar.close))
        self._deltas.append(int(bar.delta))
        self._volumes.append(int(bar.volume))
        if book is None:
            self._ask_px.append(None)
            self._ask_sz.append(None)
            self._bid_px.append(None)
            self._bid_sz.append(None)
        else:
            self._ask_px.append(book[0])
            self._ask_sz.append(book[1])
            self._bid_px.append(book[2])
            self._bid_sz.append(book[3])

        if self._since_entry < 10 ** 9:
            self._since_entry += 1

        remaining = ctx.session_end - ctx.ts

        if ctx.position != 0:
            if self._entry_ts is None:
                self._entry_ts = ctx.ts
            held = ctx.ts - self._entry_ts
            if held >= self.max_hold_ns or remaining <= self.flat_margin_ns:
                self._flatten(ctx)
            return
        self._entry_ts = None

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if self._entries >= self.max_entries:
            return
        if self._since_entry < self.min_spacing_bars:
            return
        if remaining <= self.session_cutoff_ns:
            return
        if ctx.ts - ctx.session_start < self.open_skip_ns:
            return

        direction, prior_size = self._signal()
        if direction == 0:
            return

        price = float(ctx.last_price)
        if price <= 0.0:
            return

        target_frac = min(
            max(self.target_mult * prior_size, self.min_target), self.max_target
        )
        stop_frac = min(
            max(self.stop_mult * prior_size, self.min_stop), self.max_stop
        )
        stop_off = price * stop_frac
        target_off = price * target_frac
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
            self._entries += 1
            self._since_entry = 0
            self._entry_ts = ctx.ts

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass

    # ---------------------------------------------------------------- helpers

    def _read_book(self, ctx):
        """(best ask, top-3 ask size, best bid, top-3 bid size) or None.

        The row may be absent, stale (over a second old reads as None), empty on
        one side, or zero-priced. All of those are ordinary states.
        """
        try:
            book = ctx.book()
        except Exception:
            return None
        if book is None:
            return None
        try:
            ask_px = book.ask_prices
            ask_sz = book.ask_sizes
            bid_px = book.bid_prices
            bid_sz = book.bid_sizes
            if len(ask_px) == 0 or len(bid_px) == 0:
                return None
            a0 = float(ask_px[0])
            b0 = float(bid_px[0])
            if a0 <= 0.0 or b0 <= 0.0:
                return None
            a_sz = float(sum(ask_sz[:3]))
            b_sz = float(sum(bid_sz[:3]))
            if a_sz <= 0.0 or b_sz <= 0.0:
                return None
            return a0, a_sz, b0, b_sz
        except Exception:
            return None

    def _signal(self):
        """(direction, size of the prior move) or (0, 0.0).

        Reads only state already on `self`; the current bar's book row is in the
        deques by the time this runs. +1 long after absorbed selling, -1 short
        after absorbed buying, 0 nothing.
        """
        mb = self.move_bars
        sb = self.stall_bars
        if len(self._closes) < mb + sb + 2:
            return 0, 0.0

        closes = list(self._closes)
        ref = closes[-1 - sb - mb]
        anchor = closes[-1 - sb]
        now = closes[-1]
        if ref <= 0.0 or anchor <= 0.0 or now <= 0.0:
            return 0, 0.0

        prior = (anchor - ref) / ref
        prior_size = abs(prior)
        if prior_size < self.min_move:
            return 0, 0.0

        stall = (now - anchor) / anchor
        if prior > 0.0:
            if stall > self.stall_cap * prior:
                return 0, 0.0
            if stall < -self.giveback_cap * prior:
                return 0, 0.0
        else:
            if stall < self.stall_cap * prior:
                return 0, 0.0
            if stall > -self.giveback_cap * prior:
                return 0, 0.0

        # Aggressive flow still leaning the way the move went: buyers still
        # lifting into a rising tape (or sellers still hitting a falling one).
        vols = list(self._volumes)[-sb:]
        dels = list(self._deltas)[-sb:]
        if len(vols) < sb or len(dels) < sb:
            return 0, 0.0
        traded = float(sum(vols))
        if traded <= 0.0:
            return 0, 0.0
        flow = float(sum(dels)) / traded
        if prior > 0.0:
            if flow < self.min_flow:
                return 0, 0.0
        else:
            if flow > -self.min_flow:
                return 0, 0.0

        # Displayed size on the side the flow is leaning on.
        if len(self._ask_sz) < sb + 1 or len(self._bid_sz) < sb + 1:
            return 0, 0.0
        ask_sizes = list(self._ask_sz)
        bid_sizes = list(self._bid_sz)
        if prior > 0.0:
            own = ask_sizes
            other = bid_sizes
        else:
            own = bid_sizes
            other = ask_sizes

        now_sz = own[-1]
        then_sz = own[-1 - sb]
        other_sz = other[-1]
        if now_sz is None or then_sz is None or other_sz is None:
            return 0, 0.0
        if now_sz <= 0.0 or then_sz <= 0.0:
            return 0, 0.0

        hist = [v for v in own[-self.book_window:] if v is not None]
        if len(hist) < self.book_min:
            return 0, 0.0
        ref_sz = median(hist)
        if ref_sz <= 0.0:
            return 0, 0.0

        # A wall, not a thin book.
        if now_sz < self.size_hold * ref_sz:
            return 0, 0.0
        # Not eaten during the bars the flow was arriving in: holds or refills.
        if now_sz < self.refill_ratio * then_sz:
            return 0, 0.0
        # The side facing the flow is the heavier side of the book right now.
        if now_sz < other_sz:
            return 0, 0.0

        return (-1 if prior > 0.0 else 1), prior_size

    def _flatten(self, ctx) -> None:
        """Cancel anything still working and go flat, before the day turns."""
        self._entry_ts = None
        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position != 0:
            ctx.close()
