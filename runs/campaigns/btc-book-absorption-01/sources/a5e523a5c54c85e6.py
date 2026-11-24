"""Absorption fade on the OKX BTC-USDT perpetual: cost-first rewrite.

The claim under test is the campaign's, unchanged: after a directional move,
aggressive flow that runs into displayed liquidity which *holds* marks a turn
more often than a continuation. The trade is always the fade -- short absorbed
buying after a rise, long absorbed selling after a fall -- never with the flow.

What the four prior trials actually say
---------------------------------------
Reading the four scores as a decomposition rather than as four separate bets:

    #0  -0.2927   ~3170 round trips   target 25bp / stop 30bp   market entry
    #1  -0.1051   ~3290 round trips   target 50bp / stop 35bp   market entry
    #2  -0.1044   ~1530 round trips   target 40bp / stop 30bp   mid-limit entry
    #3  -0.1282   ~2490 round trips   target 1.5x / stop 1.0x   market entry

If the score is the sum of per-trade returns (the units of every other number in
this interface are return-like, and the totals scale with trade count, so this
is the reading that fits), then per round trip:

    #0  -0.92 bp     #1  -0.32 bp     #2  -0.68 bp     #3  -0.52 bp

Every one of those is smaller in magnitude than the 4bp round trip that a
market-in / market-out trade pays, and comparable to the 2bp that a maker entry
plus a taker exit pays. In other words: on all four attempts the *gross* price
edge the absorption signal produced was roughly the size of the fee, and the
sign of the reported number was decided by the execution, not by the signal.
#0 with a 55%-breakeven payoff lost three times as much as #1 with a
41%-breakeven payoff over the same number of trades -- that is a payoff-shape
effect, not a signal effect, which is what a near-zero-edge strategy looks like.

That is the fact this file is built around. It does not try to make the signal
much stronger, because four attempts give no evidence about which filter
strengthens it and a fifth guess at the same surface is another coin flip. It
tries to stop paying for the signal while the signal is worth what it is worth:

  1. **Both legs maker.** Entry is a resting limit at the mid (0bp on this run);
     the target leg of the bracket is a limit (0bp). The only taker prints left
     are the stop, the 30-minute time stop and the pre-close flatten. A trade
     that reaches its target now costs nothing at all, instead of 4bp.
  2. **A wider target relative to the stop.** target 7 x sigma, stop 4.5 x
     sigma, where sigma is the mean absolute 1-minute return over the last 40
     bars. At a 5bp sigma that is 35bp/22bp -- a 39% breakeven hit rate -- and
     the levels scale with the regime instead of being fixed basis points that
     mean different things in 2023 and 2026.
  3. **"Effort without result", measured directly.** The prior versions asked
     for a move and then a stall. The claim is sharper than that: *aggressive
     volume arrived and the price did not respond to it*. So the test is over
     one window, at one time -- over the last 12 minutes, at least 25% of traded
     volume was buyer-initiated (bar.delta / bar.volume, accumulated per tick by
     the bar builder, so no tick subscription and no wall-clock cost) and the
     price rose by no more than 12bp. Big effort, no result. The mirror holds
     for absorbed selling. This is one measurement, not a move plus a stall plus
     a giveback bound, and it cannot be satisfied by a quiet tape because the
     participation test requires 1.15x the recent median volume over the same
     window.
  4. **The book is the confirmation, kept loose.** The absorbing side's top-5
     displayed size must be at least 0.75x its own median over the last 25
     snapshots (it did not get eaten) and at least 0.80x the other side's size
     (supply is comparable to demand, not a thin book being swept). #3 used
     0.85/0.80 and additionally required the same ratio against a sample five
     bars old; that triple conjunction is a lot of fitted surface for a claim
     that is supposed to be simple, and it is the most likely reason #3 traded
     2,500 times into nothing.
  5. **The touch-price test stays dropped.** The offer necessarily tracks the
     price, so "the ask did not lift" is implied by "the price did not rise" and
     filters nothing.

Execution, and why each choice:

  * Entry limit **at the mid**, cancelled after 2 bars. The mid sits inside the
    touch with no displayed size ahead of it, so it fills on the next print
    through it -- which on BTC is nearly every print -- at half a spread better
    than a market order and for free. #2 used exactly this and was the best of
    the four, marginally; the adverse selection it buys is real, so the patience
    is short and the signal must be fresh when the order is placed.
  * One position at a time, no reversal. A new signal while a position is open
    is ignored, so every trade opens from flat and closes to flat.
  * 30-bar time stop, 3 filled entries and 6 attempts per session, at least 6
    bars apart, none with less than 40 minutes left in the UTC day. That is
    ~1000-2500 round trips over 960 sessions -- on most days, as the campaign
    asks, without the 6,500-trade churn where the fees were the result.
  * Flat 5 minutes before `ctx.session_end` with everything cancelled, so no
    position is ever carried into the 00:00 UTC funding print, and `emit_empty`
    guarantees a callback to do it on.
  * `ctx.actionable` checked before every order, `None` from buy/sell treated as
    "no order exists", the book read defensively (a missing row, a stale row and
    an empty side are all ordinary), and every rolling window kept on `self`
    because `ctx.book()` has no history and `ctx.history()` is bounded.
"""

from __future__ import annotations

from collections import deque
from statistics import median

NS_PER_SEC = 1_000_000_000


class Strategy:
    """Fade aggressive flow that the visible book absorbed. Maker on both legs."""

    def __init__(
        self,
        interval: str = "1m",
        size: int = 100,
        flow_bars: int = 12,
        min_flow: float = 0.25,
        max_resp: float = 0.0012,
        max_adverse: float = 0.0020,
        vol_mult: float = 1.15,
        vol_base_bars: int = 150,
        book_window: int = 25,
        book_min: int = 10,
        size_hold: float = 0.75,
        size_vs_other: float = 0.80,
        target_mult: float = 7.0,
        stop_mult: float = 4.5,
        min_target: float = 0.0025,
        max_target: float = 0.0120,
        min_stop: float = 0.0015,
        max_stop: float = 0.0070,
        max_spread_frac: float = 0.0010,
        max_hold_bars: int = 30,
        entry_patience: int = 2,
        max_entries: int = 3,
        max_attempts: int = 6,
        min_spacing_bars: int = 6,
        flat_margin_s: int = 300,
        min_time_left_s: int = 2400,
    ) -> None:
        self.interval = interval
        self.size = max(1, int(size))
        self.flow_bars = max(3, int(flow_bars))
        self.min_flow = float(min_flow)
        self.max_resp = float(max_resp)
        self.max_adverse = float(max_adverse)
        self.vol_mult = float(vol_mult)
        self.vol_base_bars = max(30, int(vol_base_bars))
        self.book_window = max(5, int(book_window))
        self.book_min = max(3, int(book_min))
        self.size_hold = float(size_hold)
        self.size_vs_other = float(size_vs_other)
        self.target_mult = float(target_mult)
        self.stop_mult = float(stop_mult)
        self.min_target = float(min_target)
        self.max_target = float(max_target)
        self.min_stop = float(min_stop)
        self.max_stop = float(max_stop)
        self.max_spread_frac = float(max_spread_frac)
        self.max_hold_bars = max(2, int(max_hold_bars))
        self.entry_patience = max(1, int(entry_patience))
        self.max_entries = max(1, int(max_entries))
        self.max_attempts = max(1, int(max_attempts))
        self.min_spacing_bars = max(0, int(min_spacing_bars))
        self.flat_margin_ns = max(1, int(flat_margin_s)) * NS_PER_SEC
        self.min_time_left_ns = max(1, int(min_time_left_s)) * NS_PER_SEC

        # Everything the signal compares over time lives here: ctx.book() has no
        # history at all and ctx.history() is a bounded push window.
        self._closes: deque[float] = deque(maxlen=210)
        self._vols: deque[float] = deque(maxlen=210)
        self._deltas: deque[float] = deque(maxlen=210)
        self._ask_sz: deque[float | None] = deque(maxlen=64)
        self._bid_sz: deque[float | None] = deque(maxlen=64)

        self._session: int | None = None
        self._entries = 0
        self._attempts = 0
        self._hold = 0
        self._pending = 0
        self._bars_since_entry = 10 ** 9

    # ------------------------------------------------------------------ wiring

    def setup(self, ctx) -> None:
        # emit_empty so the pre-close flatten always has a callback to fire on.
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
            self._hold = 0
            self._pending = 0
            self._bars_since_entry = 10 ** 9

        if self._bars_since_entry < 10 ** 9:
            self._bars_since_entry += 1

        book = self._read_book(ctx)

        # Accumulate on every bar, warmup included: nothing here is fitted and
        # the warmup bars only prime the rolling windows.
        self._closes.append(float(bar.close))
        self._vols.append(float(bar.volume))
        self._deltas.append(float(bar.delta))
        if book is None:
            self._ask_sz.append(None)
            self._bid_sz.append(None)
        else:
            self._ask_sz.append(book[0])
            self._bid_sz.append(book[1])

        remaining = ctx.session_end - ctx.ts

        # --- an open position: the bracket owns the price exits; only the time
        #     stop and the day boundary are left to do here.
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
            self._pending = 0
            return

        # --- a resting entry that has not filled yet
        if ctx.working_order_ids:
            self._pending += 1
            if self._pending > self.entry_patience:
                ctx.cancel_all()
                self._pending = 0
            return
        self._pending = 0

        if book is None:
            return
        a_sz, b_sz, ask, bid = book
        if ask <= 0.0 or bid <= 0.0 or ask <= bid:
            return
        mid = 0.5 * (ask + bid)
        if mid <= 0.0:
            return
        if (ask - bid) > self.max_spread_frac * mid:
            return
        last = float(ctx.last_price)
        if last <= 0.0 or abs(mid - last) > 0.01 * last:
            return

        if self._entries >= self.max_entries:
            return
        if self._attempts >= self.max_attempts:
            return
        if self._bars_since_entry < self.min_spacing_bars:
            return
        if remaining < self.min_time_left_ns:
            return

        direction = self._signal()
        if direction == 0:
            return

        sigma = self._sigma()
        if sigma <= 0.0:
            return
        target_frac = min(
            max(self.target_mult * sigma, self.min_target), self.max_target
        )
        stop_frac = min(
            max(self.stop_mult * sigma, self.min_stop), self.max_stop
        )
        target_off = mid * target_frac
        stop_off = mid * stop_frac
        if target_off <= 0.0 or stop_off <= 0.0:
            return

        # A resting limit at the mid: inside the touch, nothing displayed ahead
        # of it, maker on the entry and on the bracket's target leg.
        if direction > 0:
            order_id = ctx.buy(
                size=self.size,
                type="limit",
                limit=mid,
                stop_offset=stop_off,
                target_offset=target_off,
                tif="DAY",
                tag="absorb-long",
            )
        else:
            order_id = ctx.sell(
                size=self.size,
                type="limit",
                limit=mid,
                stop_offset=stop_off,
                target_offset=target_off,
                tif="DAY",
                tag="absorb-short",
            )
        # None means the action cap dropped it: no order exists.
        if order_id is not None:
            self._attempts += 1
            self._pending = 0

    # --------------------------------------------------------------- callbacks

    def on_fill(self, ctx) -> None:
        # Only an entry fill leaves a non-zero position behind it.
        if ctx.fill.position != 0:
            self._entries += 1
            self._bars_since_entry = 0
            self._hold = 0
            self._pending = 0

    def on_lifecycle(self, ctx) -> None:
        # Nothing held across callbacks: orders are found by the engine, not by
        # an id kept on self, so a DAY expiry cannot leave a stale reference.
        pass

    # ----------------------------------------------------------------- helpers

    def _read_book(self, ctx):
        """(top-5 ask size, top-5 bid size, best ask, best bid) or None.

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
            ask_px = book.ask_prices
            ask_sz = book.ask_sizes
            bid_px = book.bid_prices
            bid_sz = book.bid_sizes
            if len(ask_px) == 0 or len(bid_px) == 0:
                return None
            if len(ask_sz) == 0 or len(bid_sz) == 0:
                return None
            a0 = float(ask_px[0])
            b0 = float(bid_px[0])
            if a0 <= 0.0 or b0 <= 0.0 or a0 <= b0:
                return None
            a_sz = float(sum(float(x) for x in ask_sz[:5]))
            b_sz = float(sum(float(x) for x in bid_sz[:5]))
            if a_sz <= 0.0 or b_sz <= 0.0:
                return None
            return a_sz, b_sz, a0, b0
        except Exception:
            return None

    def _sigma(self) -> float:
        """Mean absolute 1-bar return over the recent window: the bracket unit."""
        closes = list(self._closes)
        n = min(len(closes), 40)
        if n < 10:
            return 0.0
        window = closes[-n:]
        total = 0.0
        count = 0
        for i in range(1, len(window)):
            prev = window[i - 1]
            if prev > 0.0:
                diff = window[i] - prev
                total += diff if diff >= 0.0 else -diff
                count += 1
        if count == 0:
            return 0.0
        return total / count

    def _signal(self) -> int:
        """-1 short after absorbed buying, +1 long after absorbed selling, 0 none.

        Effort without result over one window: aggressive flow strongly one-sided
        and the price did not respond to it, with displayed size on the side the
        flow ran into still there.
        """
        w = self.flow_bars
        if len(self._closes) < w + 2:
            return 0

        closes = list(self._closes)
        now = closes[-1]
        ref = closes[-1 - w]
        if now <= 0.0 or ref <= 0.0:
            return 0
        ret = (now - ref) / ref

        vols = list(self._vols)
        dels = list(self._deltas)
        if len(vols) < w or len(dels) < w:
            return 0
        traded = float(sum(vols[-w:]))
        if traded <= 0.0:
            return 0
        flow = float(sum(dels[-w:])) / traded

        if flow >= self.min_flow:
            # Heavy buying. Price rose no more than max_resp (and may even have
            # fallen, which is stronger absorption) -- the offer held.
            if ret > self.max_resp or ret < -self.max_adverse:
                return 0
            absorbing = True
        elif flow <= -self.min_flow:
            if ret < -self.max_resp or ret > self.max_adverse:
                return 0
            absorbing = False
        else:
            return 0

        # Unusually large participation over the same window, normalised by the
        # recent median bar volume -- not fitted, just "more than usual".
        n = min(len(vols), self.vol_base_bars)
        if n < 2 * w:
            return 0
        base = median(vols[-n:]) * w
        if base <= 0.0 or traded < self.vol_mult * base:
            return 0

        # Displayed liquidity on the side the flow ran into held or refilled.
        sizes = self._ask_sz if absorbing else self._bid_sz
        other = self._bid_sz if absorbing else self._ask_sz
        hist = [x for x in list(sizes)[-self.book_window:] if x is not None]
        if len(hist) < self.book_min:
            return 0
        now_sz = sizes[-1]
        oth_sz = other[-1]
        if now_sz is None or oth_sz is None:
            return 0
        if now_sz <= 0.0 or oth_sz <= 0.0:
            return 0
        ref_sz = median(hist)
        if ref_sz <= 0.0:
            return 0
        # A level that survived the flow rather than being eaten.
        if now_sz < self.size_hold * ref_sz:
            return 0
        # And the side facing the flow is comparable to the side with it.
        if now_sz < self.size_vs_other * oth_sz:
            return 0

        return -1 if absorbing else 1

    def _flatten(self, ctx) -> None:
        """Cancel anything still working and go flat inside the UTC day."""
        self._hold = 0
        self._pending = 0
        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            ctx.cancel_all()
        if ctx.position != 0:
            ctx.close()
